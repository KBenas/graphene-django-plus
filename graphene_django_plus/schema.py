import functools

from django.db import models
import graphene
from graphene_django.compat import (
    ArrayField,
    HStoreField,
    JSONField,
    PGJSONField,
    RangeField,
)
from graphene_django.registry import get_global_registry

_registry = get_global_registry()


class FieldKind(graphene.Enum):
    """Field kind."""

    ID = "id"
    JSON = "json"
    STRING = "string"
    TEXT = "text"
    BOOLEAN = "boolean"
    INTEGER = "integer"
    DECIMAL = "decimal"
    FLOAT = "float"
    DATE = "date"
    DATETIME = "datetime"
    TIME = "time"
    PERCENT = "percent"
    EMAIL = "email"
    SLUG = "slug"
    PHONE = "phone"
    UUID = "uuid"
    IP = "ip"
    URL = "url"
    FILE = "file"
    PASSWORD = "password"
    CURRENCY = "currency"
    POSTAL_CODE = "postal-code"
    COMPANY_DOCUMENT = "company-document"
    INDIVIDUAL_DOCUMENT = "individual-document"


@functools.singledispatch
def get_field_schema(field, registry=None) -> dict:
    raise Exception(f"Don't know how to convert the Django field {field} ({field.__class__})")


@get_field_schema.register(models.CharField)
def get_field_schema_string(field, registry=None):
    return {
        "kind": FieldKind.STRING,
    }


@get_field_schema.register(models.TextField)
def get_field_schema_text(field, registry=None):
    return {
        "kind": FieldKind.TEXT,
    }


@get_field_schema.register(models.EmailField)
def get_field_schema_email(field, registry=None):
    return {
        "kind": FieldKind.EMAIL,
    }


@get_field_schema.register(models.SlugField)
def get_field_schema_slug(field, registry=None):
    return {
        "kind": FieldKind.SLUG,
    }


@get_field_schema.register(models.UUIDField)
def get_field_schema_uuid(field, registry=None):
    return {
        "kind": FieldKind.UUID,
    }


@get_field_schema.register(models.URLField)
def get_field_schema_url(field, registry=None):
    return {
        "kind": FieldKind.URL,
    }


@get_field_schema.register(models.GenericIPAddressField)
def get_field_schema_ip(field, registry=None):
    return {
        "kind": FieldKind.IP,
    }


@get_field_schema.register(models.FileField)
@get_field_schema.register(models.ImageField)
@get_field_schema.register(models.FilePathField)
def get_field_schema_file(field, registry=None):
    return {
        "kind": FieldKind.FILE,
    }


@get_field_schema.register(models.AutoField)
@get_field_schema.register(models.PositiveIntegerField)
@get_field_schema.register(models.PositiveSmallIntegerField)
@get_field_schema.register(models.SmallIntegerField)
@get_field_schema.register(models.BigIntegerField)
@get_field_schema.register(models.IntegerField)
def get_field_schema_int(field, registry=None):
    return {
        "kind": FieldKind.INTEGER,
    }


@get_field_schema.register(models.DecimalField)
@get_field_schema.register(models.DurationField)
def get_field_schema_decimal(field, registry=None):
    return {
        "kind": FieldKind.DECIMAL,
        "validation": {
            "max_digits": field.max_digits,
            "decimal_places": field.decimal_places,
        },
    }


@get_field_schema.register(models.FloatField)
def get_field_schema_float(field, registry=None):
    return {
        "kind": FieldKind.FLOAT,
    }


@get_field_schema.register(models.BooleanField)
def get_field_schema_bool(field, registry=None):
    return {
        "kind": FieldKind.BOOLEAN,
    }


if hasattr(models, "NullBooleanField"):

    @get_field_schema.register(models.NullBooleanField)  # type:ignore
    def get_field_schema_nullbool(field, registry=None):
        return {
            "kind": FieldKind.BOOLEAN,
        }


@get_field_schema.register(models.DateField)
def get_field_schema_date(field, registry=None):
    return {
        "kind": FieldKind.DATE,
    }


@get_field_schema.register(models.DateTimeField)
def get_field_schema_datetime(field, registry=None):
    return {
        "kind": FieldKind.DATETIME,
    }


@get_field_schema.register(models.TimeField)
def get_field_schema_time(field, registry=None):
    return {
        "kind": FieldKind.TIME,
    }


@get_field_schema.register(models.ForeignKey)
@get_field_schema.register(models.OneToOneRel)
@get_field_schema.register(models.OneToOneField)
def get_field_schema_fk(field, registry=None):
    model = field.related_model
    registry = registry or _registry
    _type = registry.get_type_for_model(model)
    return {
        "kind": FieldKind.ID,
        "of_type": _type._meta.name if _type else None,
    }


@get_field_schema.register(models.ManyToManyField)
@get_field_schema.register(models.ManyToManyRel)
@get_field_schema.register(models.ManyToOneRel)
def get_field_schema_m2m(field, registry=None):
    model = field.related_model
    registry = registry or _registry
    _type = registry.get_type_for_model(model)
    return {
        "kind": FieldKind.ID,
        "of_type": _type._meta.name if _type else None,
        "multiple": True,
    }


@get_field_schema.register(ArrayField)
@get_field_schema.register(RangeField)
def get_field_schema_array(field, registry=None):
    d = get_field_schema(field.base_field)
    d["multiple"] = True
    return d


@get_field_schema.register(PGJSONField)
@get_field_schema.register(JSONField)
@get_field_schema.register(HStoreField)
def get_field_schema_pg(field, registry=None):
    return {
        "kind": FieldKind.JSON,
    }
