import os
import json
import yaml
import base64
import mimetypes
import csv
from io import StringIO
from pathlib import Path
from typing import Dict, Any, List
import kitebase
from kitebase.endpoints import endpoint


@endpoint('read_file')
def read_file(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generic endpoint for reading files of various formats.

    This endpoint allows clients to read files from server-side directories that are
    configured as allowed in the Kitebase configuration. It supports multiple file
    formats including structured data (JSON, YAML, CSV) and binary files.

    Parameters:
        - file_path: Path to the file to be read (relative or absolute)
        - base_dir: Base directory (optional, used for relative paths)
        - format: File format ('auto', 'json', 'yaml', 'csv', 'text', 'binary')
          If 'auto' is specified (default), the format will be determined from the file extension
        - binary_encoding: For binary files, encoding type ('base64', 'hex')
        - csv_options: Options for CSV parsing:
          - delimiter: Character used as field separator (default: ',')
          - has_header: Whether CSV has a header row (default: True)

    Returns:
        Dictionary with:
        - status: 'success' or 'error'
        - data: Parsed file content
        - file_info: Metadata about the file (name, path, type, etc.)
        - code: HTTP status code

    Configuration in config.yaml:
        read_files:
          allowed_dirs: Directories that can be read, relative to the app or
                        absolute. Empty or absent: nothing can be read.
          text_suffix: List of file extensions to be treated as text

    Examples:
        # Reading a JSON configuration file
        {
            "operation": "read_file",
            "parameters": {
                "file_path": "configs/app_settings.json"
            }
        }

        # Reading a CSV file with custom delimiter
        {
            "operation": "read_file",
            "parameters": {
                "file_path": "data/users.csv",
                "csv_options": {
                    "delimiter": ";",
                    "has_header": true
                }
            }
        }

        # Reading an image file (binary)
        {
            "operation": "read_file",
            "parameters": {
                "file_path": "images/logo.png",
                "binary_encoding": "base64"
            }
        }

        # Specifying a base directory
        {
            "operation": "read_file",
            "parameters": {
                "base_dir": "/var/www/html",
                "file_path": "assets/styles.css"
            }
        }
    """
    try:
        # Extract and validate file path
        file_path = data.get('file_path')
        if not file_path:
            return {"status": "error", "message": "File path is required", "code": 400}

        app = kitebase.utils.get_app()
        pm = app.pm
        file_config = pm.config.get('read_files', {})
        text_suffixes = file_config.get('text_suffix', ['.txt', '.md', '.xml', '.html', '.css', '.js'])

        # Relative paths hang from the application directory, like every other
        # path config.yaml declares - not from wherever the process was started.
        allowed_dirs = [pm.resolve_path(os.path.expanduser(d))
                        for d in file_config.get('allowed_dirs', [])]

        # Closed unless configured: an endpoint that hands out files must not
        # hand out every file the process can read, `.env` included.
        if not allowed_dirs:
            return {"status": "error", "code": 403,
                    "message": "No file can be read: read_files.allowed_dirs is empty"}

        base_dir = data.get('base_dir')
        full_path = os.path.join(os.path.expanduser(base_dir), file_path) if base_dir else file_path

        # resolve() follows symlinks and '..', so the check below sees the real target
        path = pm.resolve_path(os.path.expanduser(full_path))

        if not is_path_allowed(path, allowed_dirs):
            return {"status": "error", "code": 403,
                    "message": "Access to this file is not allowed"}

        # Check if the file exists
        if not path.exists() or not path.is_file():
            return {"status": "error", "message": f"File not found: {file_path}", "code": 404}

        # Determine the format
        format_type = data.get('format', 'auto').lower()
        if format_type == 'auto':
            suffix = path.suffix.lower()
            if suffix in ['.yaml', '.yml']:
                format_type = 'yaml'
            elif suffix == '.json':
                format_type = 'json'
            elif suffix == '.csv':
                format_type = 'csv'
            elif suffix in text_suffixes:
                format_type = 'text'
            else:
                # Assume it's a binary file
                format_type = 'binary'

        # Read and parse the file
        if format_type in ['yaml', 'json', 'text', 'csv']:
            # Read as text
            with open(path, 'r', encoding='utf-8') as f:
                file_content = f.read()

                if format_type == 'yaml':
                    parsed_data = yaml.safe_load(file_content)
                elif format_type == 'json':
                    parsed_data = json.loads(file_content)
                elif format_type == 'csv':
                    # Get CSV parsing options
                    csv_options = data.get('csv_options', {})
                    delimiter = csv_options.get('delimiter', ',')
                    has_header = csv_options.get('has_header', True)

                    # Parse CSV
                    csv_data = []
                    csv_file = StringIO(file_content)

                    if has_header:
                        # Parse as list of dictionaries (with headers)
                        reader = csv.DictReader(csv_file, delimiter=delimiter)
                        csv_data = list(reader)
                    else:
                        # Parse as list of lists (no headers)
                        reader = csv.reader(csv_file, delimiter=delimiter)
                        csv_data = list(reader)

                    parsed_data = csv_data
                else:  # text
                    parsed_data = file_content
        else:  # binary
            # Read as binary
            with open(path, 'rb') as f:
                binary_content = f.read()

                # Determine MIME type
                mime_type, _ = mimetypes.guess_type(str(path))
                if not mime_type:
                    mime_type = 'application/octet-stream'

                # Encode in base64 or hex
                binary_encoding = data.get('binary_encoding', 'base64')
                if binary_encoding == 'base64':
                    encoded_content = base64.b64encode(binary_content).decode('ascii')
                elif binary_encoding == 'hex':
                    encoded_content = binary_content.hex()
                else:
                    return {"status": "error",
                            "message": f"Unsupported binary encoding: {binary_encoding}", "code": 400}

                parsed_data = {
                    "content": encoded_content,
                    "mime_type": mime_type,
                    "encoding": binary_encoding,
                    "size": len(binary_content)
                }

        # Add file metadata
        try:
            relative_path = path.relative_to(Path.cwd())
        except ValueError:
            relative_path = path

        file_info = {
            "filename": path.name,
            "extension": path.suffix,
            "content_type": mimetypes.guess_type(str(path))[0] or "application/octet-stream",
            "path": str(relative_path),
            "format": format_type,
            "last_modified": os.path.getmtime(path),
            "size": os.path.getsize(path)
        }

        return {
            "status": "success",
            "data": parsed_data,
            "file_info": file_info,
            "code": 200
        }

    except yaml.YAMLError as e:
        return {"status": "error", "message": f"YAML parsing error: {str(e)}", "code": 400}
    except json.JSONDecodeError as e:
        return {"status": "error", "message": f"JSON parsing error: {str(e)}", "code": 400}
    except csv.Error as e:
        return {"status": "error", "message": f"CSV parsing error: {str(e)}", "code": 400}
    except UnicodeDecodeError:
        return {"status": "error", "message": "File appears to be binary, but was requested as text", "code": 400}
    except PermissionError:
        return {"status": "error", "message": f"Permission denied reading file: {file_path}", "code": 403}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": str(e), "code": 500}


def is_path_allowed(path: Path, allowed_dirs: List[Path]) -> bool:
    """
    True if `path` lies inside one of `allowed_dirs`.

    Compared by path components, not by string prefix: `data` must not let
    `database/` or `data-private/` through. Both sides are expected resolved.
    """
    return any(path.is_relative_to(allowed) for allowed in allowed_dirs)
