import logging
import time
import uuid
import traceback as _traceback
from functools import wraps
from typing import Dict, List, Any, Optional, Union, Callable
from pathlib import Path
import kitebase
import kitebase.context
from kitebase.access import UNDECLARED, AccessDenied, check_endpoint
from kitebase.plugins import load_modules
from kitebase.registry import Registry

# The endpoints of the process, by name
_ENDPOINTS: Registry[Callable] = Registry('endpoints', 'endpoint')
# What each endpoint declared with `access=`; absent means undeclared
_ACCESS: Dict[str, Optional[str]] = {}


def endpoint(name: str, access: Optional[str] = UNDECLARED, *,
             replace: bool = False) -> Callable[[Callable], Callable]:
    """
    Decorator to register an endpoint function.

    All endpoints require authentication by convention — the @endpoint system
    is designed for authenticated Kitebase clients. For truly public routes
    (e.g. login, health check) use dedicated server routes instead.

    Args:
        name: The name of the endpoint for registration
        access: What calling it requires, checked by the dispatcher before it
                runs: a permission code (`reports.run`), `PER_TABLE` when the
                endpoint checks each table it touches itself, `AUTHENTICATED`
                when any logged-in user may call it. Undeclared, the guard
                decides: the open one lets it through, a real one need not.
        replace: Take the place of the endpoint another module registered
                under this name. Without it, a name already taken is an error:
                two plugins cannot answer the same call, and the one loaded
                last would win without anyone having chosen it.

    Returns:
        The decorated function with registration side effect
    """
    def decorator(func: Callable) -> Callable:
        _ENDPOINTS.add(name, func, replace=replace)
        _ACCESS[name] = access

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)
        return wrapper
    return decorator


class FileResult:
    """
    What an endpoint returns when its answer is a file rather than data.

    It leaves through `{prefix}/download/{op}`, as the bytes themselves; the
    dispatcher answers JSON only and refuses it, so an endpoint is called by
    one route or the other, never both.

    Args:
        content:     the bytes of the file
        filename:    the name the browser saves it under
        mime:        its media type
        disposition: 'attachment' to save it, 'inline' to open it (a PDF to
                     preview)
    """
    def __init__(self, content: bytes, filename: str,
                 mime: str = 'application/octet-stream',
                 disposition: str = 'attachment') -> None:
        if disposition not in ('attachment', 'inline'):
            raise ValueError(f"disposition must be 'attachment' or 'inline', not {disposition!r}")
        self.content = content
        self.filename = filename
        self.mime = mime
        self.disposition = disposition


class CommandResult:
    """
    Class to represent the result of a command execution.

    This class provides a standardized format for command responses,
    including success/error status, data payload, and metadata.
    """
    def __init__(self,
                 status: str = "success",
                 data: Any = None,
                 message: Optional[str] = None,
                 request_id: Optional[str] = None,
                 code: int = 200,
                 error_type: Optional[str] = None,
                 traceback: Optional[str] = None,
                 flow: Optional[Dict[str, Any]] = None) -> None:
        """
        Initialize a command result.

        Args:
            status: The status of the result ("success" or "error")
            data: The payload: the result, or what a refusal has to say about it
            message: Error message (for error status)
            request_id: Unique identifier of the request
            code: Status code (similar to HTTP status codes)
            error_type: Exception class name (e.g. "ValueError")
            traceback: Full Python traceback string (always included for errors)
            flow: What the rules of a write had to say (kitebase.flow): a
                  question, lines for the person, actions for the client
        """
        self.status = status
        self.data = data
        self.message = message
        self.request_id = request_id
        self.code = code
        self.error_type = error_type
        self.traceback = traceback
        self.flow = flow
        self.timestamp = int(time.time())

    def to_dict(self) -> Dict[str, Any]:
        """
        Convert the result to a dictionary.

        Returns:
            Dictionary representation of the command result
        """
        result = {
            "status": self.status,
            "code": self.code,
            "timestamp": self.timestamp,
        }

        if self.request_id:
            result["request_id"] = self.request_id

        if self.status == "success":
            result["data"] = self.data
            # A success may have something to say — an endpoint called from a
            # button reports what it did, and the envelope already has the field.
            if self.message:
                result["message"] = self.message
        else:
            result["message"] = self.message or "Unknown error"
            # A refusal can say what to fix: the field errors of a form, the
            # detail of a report. The client reads them from `data` either way.
            if self.data is not None:
                result["data"] = self.data
            if self.error_type:
                result["error_type"] = self.error_type
            if self.traceback:
                result["traceback"] = self.traceback

        # Either way: a question is a refusal, and an action follows a success.
        if self.flow:
            result["$flow"] = self.flow

        return result

    def to_json(self) -> str:
        """
        Convert the result to a JSON string.

        Returns:
            JSON string representation of the command result
        """
        import json
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'CommandResult':
        """
        Create a result from a dictionary.

        Args:
            data: Dictionary containing command result data

        Returns:
            A new CommandResult instance
        """
        return cls(status=data.get("status", "success"),
                   data=data.get("data"),
                   message=data.get("message"),
                   request_id=data.get("request_id"),
                   code=data.get("code", 200),
                   error_type=data.get("error_type"),
                   traceback=data.get("traceback"))

    @classmethod
    def from_json(cls, json_str: str) -> 'CommandResult':
        """
        Create a result from a JSON string.

        Args:
            json_str: JSON string containing command result data

        Returns:
            A new CommandResult instance
        """
        import json
        data = json.loads(json_str)
        return cls.from_dict(data)


_log = logging.getLogger('kitebase')


def _param_names(parameters: Any) -> str:
    """The shape of a request without its values: keys, and keys of `data`."""
    if not isinstance(parameters, dict):
        return type(parameters).__name__
    parts = []
    for key, value in parameters.items():
        if isinstance(value, dict):
            parts.append(f"{key}{{{','.join(map(str, value))}}}")
        elif isinstance(value, (list, tuple)):
            parts.append(f"{key}[{len(value)}]")
        else:
            parts.append(str(key))
    return ' '.join(parts)


class Command:
    """
    Class to represent a command to be processed.

    This class encapsulates all information needed to execute a command,
    including operation name, parameters, execution metadata, and authentication context.
    """
    def __init__(self,
                 operation: str,
                 parameters: Optional[Dict[str, Any]] = None,
                 request_id: Optional[str] = None,
                 context: Optional[Dict[str, Any]] = None,
                 version: str = "1.0") -> None:
        """
        Initialize a command.

        Args:
            operation: The name of the operation to execute
            parameters: Parameters to pass to the operation
            request_id: Unique identifier for the command (auto-generated if None)
            context: Execution context (tenant, user, permissions, etc.)
            version: API version string
        """
        self.operation = operation
        self.parameters = parameters or {}
        self.request_id = request_id or str(uuid.uuid4())
        # Empty rather than None: the dispatcher sets it on every command, and an
        # empty context is what clears the one left by the previous request.
        self.context = context or {}
        self.version = version

    @classmethod
    def from_json(cls, json_str: str) -> 'Command':
        """
        Create a command from a JSON string.

        Args:
            json_str: JSON string containing command data

        Returns:
            A new Command instance
        """
        import json
        data = json.loads(json_str)
        return cls.from_dict(data)

    def to_json(self) -> str:
        """
        Convert the command to a JSON string.

        Returns:
            JSON string representation of the command
        """
        import json
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Command':
        """
        Create a command from a dictionary.
        """
        return cls(operation=data.get("operation", ""),
                   parameters=data.get("parameters", {}),
                   request_id=data.get("request_id"),
                   context=data.get("context"),
                   version=data.get("version", "1.0"))

    def to_dict(self) -> Dict[str, Any]:
        """
        Convert the command to a dictionary.
        """
        return {
            "operation": self.operation,
            "parameters": self.parameters,
            "request_id": self.request_id,
            "context": self.context,
            "version": self.version,
        }


class CommandProcessor:
    """
    Processes commands by routing them to registered endpoints.

    One command in, one result out, on the calling thread. It used to run each
    command in a thread of its own and hand back a `request_id` to collect the
    result with — but every caller passed `wait=True` and blocked on it, so the
    thread bought no concurrency; it only kept a dictionary of results alive.
    Concurrency belongs to the web server (a thread per request), and operations
    that genuinely outlive a request belong in a subprocess, where they can also
    be killed — see `docs/pending/jobs.md`.
    """

    def __init__(self) -> None:
        """
        Initialize the command processor.

        Takes what is registered at this point: the package's own endpoints
        (`auth`, `db`, `query`, `get_page` and the rest are decorated at import
        time) and those of the plugin modules already loaded.
        """
        self.endpoints: Dict[str, Callable] = dict(_ENDPOINTS)

    def resolve_endpoints(self, file_paths: List[Union[str, Path]]) -> None:
        """
        Load Python files and take the endpoints they register.

        Args:
            file_paths: List of paths to Python files containing endpoint definitions

        Raises:
            ImportError: If a file does not load, naming the module and the cause
        """
        load_modules(file_paths)
        self.endpoints.update(_ENDPOINTS)

    def _execute_command(self, command: Command) -> CommandResult:
        """
        Route a command to its endpoint and wrap whatever comes back.

        Args:
            command: The command to execute

        Returns:
            The result, always as a CommandResult — an endpoint that raises
            produces a 500 rather than an exception escaping to the server.
        """
        # One line per request, and the traceback where it can be found again:
        # the client shows an error once and the dialog closes, the log is what
        # remains. Who listens is the application's choice (server_utils.
        # setup_logging); this only speaks. Parameter *names* at DEBUG, never
        # their values — a `db update` on a user carries the password.
        who = (command.context or {}).get('username') or '-'
        started = time.perf_counter()
        if _log.isEnabledFor(logging.DEBUG):
            _log.debug('%s by %s params=%s [%s]', command.operation, who,
                       _param_names(command.parameters), command.request_id)

        if command.operation not in self.endpoints:
            _log.warning("%s by %s: no such operation [%s]", command.operation, who,
                         command.request_id)
            return CommandResult(status="error",
                                 message=f"Operation '{command.operation}' not found",
                                 request_id=command.request_id,
                                 code=404)

        try:
            # Set the context before executing the function. Unconditional, and it
            # replaces the whole value: request threads are reused by the server's
            # pool, so anything left behind would be read by the next user.
            kitebase.context.replace(command.context)

            check_endpoint(command.operation, _ACCESS.get(command.operation, UNDECLARED))
            result_data = self.endpoints[command.operation](command.parameters)

            # An endpoint may either return its own envelope or a plain payload
            if isinstance(result_data, dict) and "status" in result_data:
                result = CommandResult(
                    status=result_data.get("status"),
                    data=result_data.get("data"),
                    message=result_data.get("message"),
                    request_id=command.request_id,
                    code=result_data.get("code", 200),
                    flow=result_data.get("$flow"),
                )
            else:
                result = CommandResult(
                    status="success",
                    data=result_data,
                    request_id=command.request_id
                )

        except AccessDenied as e:
            _log.warning('%s by %s: denied %s [%s]', command.operation, who,
                         e.permission, command.request_id)
            return CommandResult(status="error", message=str(e),
                                 request_id=command.request_id, code=403,
                                 error_type=type(e).__name__)

        except Exception as e:
            _log.exception('%s by %s failed after %dms [%s]', command.operation, who,
                           (time.perf_counter() - started) * 1000, command.request_id)
            return CommandResult(
                status="error",
                message=str(e),
                request_id=command.request_id,
                code=500,
                error_type=type(e).__name__,
                traceback=_traceback.format_exc()
            )

        # An endpoint that refused (its own error envelope) is not a failure of
        # the server, but it is something the person who asked will report.
        level = logging.INFO if result.status == 'success' else logging.WARNING
        _log.log(level, '%s by %s → %s %s in %dms [%s]', command.operation, who,
                 result.status, result.code, (time.perf_counter() - started) * 1000,
                 command.request_id)
        return result

    def send(self, command_dict: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a command and return its result.

        Args:
            command_dict: Dictionary representation of the command

        Returns:
            Dictionary with the command result
        """
        return self._execute_command(Command.from_dict(command_dict)).to_dict()
