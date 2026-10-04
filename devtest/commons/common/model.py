from kitebase.querybuilder import filters_mention


class Archivable:
    """
    Mixin that adds soft-delete (archive) behaviour to a table.

    Protocol attributes read by the querybuilder to apply automatic filtering:
      _kb_archive_field  — column name that controls visibility (default: 'active')
      _kb_archive_value  — value that means "not archived" (default: True)

    Supports different field conventions:
      active=True   → visible   (Archivable default)
      archived=True → hidden    → set _kb_archive_value = False
      visible=True  → visible   → set field name to 'visible'
    """
    _kb_archive_field: str = 'active'
    _kb_archive_value: bool = True

    def archive(self) -> None:
        """Mark record as archived (hidden from default queries)."""
        setattr(self, self._kb_archive_field, not self._kb_archive_value)

    def unarchive(self) -> None:
        """Restore record to active state."""
        setattr(self, self._kb_archive_field, self._kb_archive_value)

    @property
    def is_archived(self) -> bool:
        """True if the record is currently archived."""
        return getattr(self, self._kb_archive_field) != self._kb_archive_value

    # ── Query behavior protocol ────────────────────────────────────────────
    # Registered via app.add_query_behavior(Archivable) at startup.

    @classmethod
    def applies_to(cls, model_class) -> bool:
        """True if model_class carries the archive protocol."""
        return hasattr(model_class, '_kb_archive_field')

    @classmethod
    def apply(cls, model_class, query_def: dict, query):
        """
        Add implicit WHERE active=True unless the caller says otherwise.

        `archived` in the query definition picks the view — `'only'` for the
        archived rows, `'all'` for everything, absent for the default. It is
        what the `archivable` command sets, and a view carries it without
        reading it. `include_archived: true` is the older spelling of `'all'`.
        """
        field = getattr(model_class, '_kb_archive_field')
        value = getattr(model_class, '_kb_archive_value', True)
        archived = query_def.get('archived')
        if archived == 'all' or query_def.get('include_archived'):
            return query
        if archived == 'only':
            col = getattr(model_class, field, None)
            return query.where(col != value) if col is not None else query
        # The caller conditions the column itself: what they wrote is what
        # they want. The query builder owns the filter syntax, so it answers.
        if filters_mention(query_def.get('filters'), model_class.__name__, field):
            return query
        col = getattr(model_class, field, None)
        if col is not None:
            query = query.where(col == value)
        return query
