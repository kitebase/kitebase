from sqlalchemy import Index
from sqlalchemy.orm import validates
from sqlalchemy.ext.hybrid import hybrid_property
from kitebase.i18n import _


class Author:

    @hybrid_property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    @full_name.expression
    def full_name(cls):
        return cls.first_name + ' ' + cls.last_name

    __table_args__ = (
        Index("name", "last_name", "first_name", unique=True),
    )

    def __repr__(self):
        return f"<Author {self.full_name}>"


class Book:

    @validates('isbn')
    def validate_isbn(self, _key, value):
        if value:
            clean_isbn = value.replace('-', '')
            if not (len(clean_isbn) == 10 or len(clean_isbn) == 13):
                raise ValueError(_('ISBN must be 10 or 13 digits (dashes optional)'))
        return value

    def __repr__(self):
        return f"<Book {self.title}>"
