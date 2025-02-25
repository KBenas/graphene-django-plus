# This was based on some code from https://github.com/mirumee/saleor
# but adapted to use relay, automatic field detection and some code adjustments

import collections
import collections.abc
import itertools
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Generic,
    List,
    Optional,
    Type,
    TypeVar,
    cast,
)

from django.core.exceptions import NON_FIELD_ERRORS, ImproperlyConfigured
from django.core.exceptions import PermissionDenied as DJPermissionDenied
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models.fields import NOT_PROVIDED
from django.db.models.fields.reverse_related import ManyToManyRel, ManyToOneRel
import graphene
from graphene.relay.mutation import ClientIDMutation
from graphene.types.mutation import MutationOptions
from graphene.types.objecttype import ObjectType
from graphene.types.utils import yank_fields_from_attrs
from graphene.utils.str_converters import to_camel_case, to_snake_case
from graphene_django.registry import Registry, get_global_registry
from graphql.error import GraphQLError

from .exceptions import PermissionDenied
from .input_types import get_input_field
from .models import GuardedModel
from .perms import check_authenticated, check_perms
from .settings import graphene_django_plus_settings
from .types import (
    MutationErrorType,
    ResolverInfo,
    UploadType,
    schema_for_field,
    schema_registry,
)
from .utils import get_model_fields, get_node, get_nodes, update_dict_nested

_registry = get_global_registry()
_T = TypeVar("_T", bound=models.Model)
_M = TypeVar("_M", bound="BaseMutation")
_MM = TypeVar("_MM", bound="ModelMutation")


def _get_model_name(model):
    model_name = model.__name__
    return to_snake_case(model_name[:1].lower() + model_name[1:])


def _get_output_fields(model, return_field_name, registry):
    model_type = registry.get_type_for_model(model)
    if not model_type:  # pragma: no cover
        raise ImproperlyConfigured(
            "Unable to find type for model {} in graphene registry".format(
                model.__name__,
            )
        )
    f = graphene.Field(
        lambda: registry.get_type_for_model(model),
        description="The mutated object.",
    )
    return {return_field_name: f}


def _get_validation_errors(validation_error):
    e_list = []

    if hasattr(validation_error, "error_dict"):
        # convert field errors
        for field, field_errors in validation_error.message_dict.items():
            for e in field_errors:
                if field == NON_FIELD_ERRORS:
                    field = None
                else:
                    field = to_camel_case(field)
                e_list.append(MutationErrorType(field=field, message=e))
    else:
        # convert non-field errors
        for e in validation_error.error_list:
            e_list.append(MutationErrorType(message=e.message))

    return e_list


def _get_fields(model, only_fields, exclude_fields, required_fields, registry):
    reverse_rel_include = graphene_django_plus_settings.MUTATIONS_INCLUDE_REVERSE_RELATIONS

    ret = collections.OrderedDict()
    for name, field in get_model_fields(model):
        if (
            (only_fields and name not in only_fields)
            or name in exclude_fields
            or str(name).endswith("+")
            or name in ["created_at", "updated_at", "archived_at"]
        ):
            continue

        if name == "id":
            graphene_type = registry.get_type_for_model(model)
            description = (
                f"ID of the "
                f'"{graphene_type._meta.name if graphene_type else model.__name__}" to mutate'
            )
            f = graphene.ID(
                description=description,
            )
        else:
            # Checking whether it was globally configured to not include reverse relations
            if isinstance(field, ManyToOneRel) and not reverse_rel_include and not only_fields:
                continue

            f = get_input_field(field, registry)

        if required_fields is not None:
            required = name in required_fields
            f.kwargs["required"] = required
        else:
            if isinstance(field, (ManyToOneRel, ManyToManyRel)):
                required = not field.null
            else:
                required = not field.blank and field.default is NOT_PROVIDED

            f.kwargs["required"] = required

        s = schema_for_field(field, name, registry)
        required = f.kwargs["required"]
        s["validation"]["required"] = required

        ret[name] = {
            "field": f,
            "schema": s,
        }

    return ret


def _is_list_of_ids(field):
    return isinstance(field.type, graphene.List) and field.type.of_type == graphene.ID


def _is_id_field(field):
    return (
        field.type == graphene.ID
        or isinstance(field.type, graphene.NonNull)
        and field.type.of_type == graphene.ID
    )


def _is_upload_field(field):
    t = getattr(field.type, "of_type", field.type)
    return t == UploadType


class BaseMutationOptions(MutationOptions):
    """Model type options for :class:`BaseMutation` and subclasses."""

    #: A list of Django permissions to check against the user
    permissions: Optional[List[str]] = None

    #: If any permission should allow the user to execute this mutation
    permissions_any: bool = True

    #: If we should allow unauthenticated users to do this mutation
    public: bool = False

    #: The input schema for the schema query
    input_schema: Optional[dict] = None

    #: Optional registry to register/retrieve types and fields instead of the global one
    registry: Optional[Registry] = None


class BaseMutation(ClientIDMutation):
    """Base mutation enhanced with permission checking and relay id handling."""

    class Meta:
        abstract = True

    if TYPE_CHECKING:

        @classmethod
        @property
        def _meta(cls) -> BaseMutationOptions:
            ...

    #: A list of errors that happened during the mutation
    errors = graphene.List(
        graphene.NonNull(MutationErrorType),
        description="List of errors that occurred while executing the mutation.",
    )

    @classmethod
    def __class_getitem__(cls, *args, **kwargs):
        return cls

    @classmethod
    def __init_subclass_with_meta__(
        cls,
        permissions=None,
        permissions_any=True,
        public=False,
        input_schema=None,
        registry=None,
        _meta=None,
        **kwargs,
    ):
        if not _meta:
            _meta = BaseMutationOptions(cls)
        if "allow_unauthenticated" in kwargs:
            raise ImproperlyConfigured("Use 'public' instead of 'allow_unauthenticated'")

        _meta.permissions = permissions or []
        _meta.permissions_any = permissions_any
        _meta.public = public
        _meta.input_schema = input_schema or {}
        _meta.registry = registry or _registry

        super().__init_subclass_with_meta__(_meta=_meta, **kwargs)

        iname = cls.Input._meta.name
        schema_registry[iname] = {
            "object_type": iname,
            "fields": list(_meta.input_schema.values()),
        }

    @classmethod
    def get_node(
        cls,
        info: ResolverInfo,
        node_id: str,
        field: str = "id",
        only_type: Optional[ObjectType] = None,
    ) -> Any:
        """Get the node object given a relay global id."""
        if not node_id:
            return None

        try:
            node = get_node(info, node_id, graphene_type=only_type, registry=cls._meta.registry)
        except (AssertionError, GraphQLError) as e:
            raise ValidationError({field: str(e)})
        else:
            if node is None:  # pragma: no cover
                raise ValidationError({field: f"Couldn't resolve to a node: {node_id}"})

        return node

    @classmethod
    def get_nodes(
        cls,
        info: ResolverInfo,
        ids: List[str],
        field: str = "ids",
        only_type: Optional[ObjectType] = None,
    ) -> List[Any]:
        """Get a list of node objects given a list of relay global ids."""
        try:
            instances = get_nodes(info, ids, graphene_type=only_type, registry=cls._meta.registry)
        except GraphQLError as e:
            raise ValidationError({field: str(e)})

        return instances

    @classmethod
    def check_permissions(cls, info: ResolverInfo) -> bool:
        """Check permissions for the given user.

        Subclasses can override this to avoid the permission checking or
        extending it. Remember to call `super()` in the later case.

        """
        user = info.context.user

        if not cls._meta.public and not check_authenticated(user):
            return False

        if not cls._meta.permissions:
            return True

        return check_perms(user, cls._meta.permissions, any_perm=cls._meta.permissions_any)

    @classmethod
    def mutate_and_get_payload(cls: Type[_M], root, info: ResolverInfo, **data) -> _M:
        """Mutate checking permissions.

        We override the default graphene's method to call
        :meth:`.check_permissions` and populate :attr:`.errors` in case
        of errors automatically.

        The mutation itself should be defined in :meth:`.perform_mutation`.

        """
        try:
            if not cls.check_permissions(info):
                raise PermissionDenied()

            response = cls.perform_mutation(root, info, **data)
            if response.errors is None:
                response.errors = []
            return response
        except ValidationError as e:
            errors = _get_validation_errors(e)
            return cls(errors=errors)
        except DJPermissionDenied as e:
            if not graphene_django_plus_settings.MUTATIONS_SWALLOW_PERMISSION_DENIED:
                raise
            msg = str(e) or "Permission denied..."
            return cls(errors=[MutationErrorType(message=msg)])

    @classmethod
    def perform_mutation(cls: Type[_M], root, info: ResolverInfo, **data) -> _M:
        """Perform the mutation.

        This should be implemented in subclasses to perform the
        mutation.

        """
        raise NotImplementedError


class ModelMutationOptions(BaseMutationOptions, Generic[_T]):
    """Model type options for :class:`BaseModelMutation` and subclasses."""

    #: The Django model.
    model: Type[_T]

    #: A list of guardian object permissions to check if the user has
    #: permission to perform a mutation to the model object.
    object_permissions: Optional[List[str]] = None

    #: If any object permission should allow the user to perform the mutation.
    object_permissions_any: bool = True

    #: Exclude the given fields from the mutation input.
    exclude_fields: Optional[List[str]] = None

    #: Include only those fields in the mutation input.
    only_fields: Optional[List[str]] = None

    #: Mark those fields as required (note that fields marked with `null=False`
    #: in Django will already be considered required).
    required_fields: Optional[List[str]] = None

    #: The name of the field that will contain the object type. If not
    #: provided, it will default to the model's name.
    return_field_name: Optional[str] = None


class BaseModelMutation(BaseMutation, Generic[_T]):
    """Base mutation for models.

    This will allow mutations for both create and update operations,
    depending on if the object's id is present in the input or not.

    See :class:`ModelMutationOptions` for a list of meta configurations.

    """

    class Meta:
        abstract = True

    if TYPE_CHECKING:

        @classmethod
        @property
        def _meta(cls) -> ModelMutationOptions[_T]:
            ...

    @classmethod
    def __init_subclass_with_meta__(
        cls,
        model=None,
        object_permissions=None,
        object_permissions_any=True,
        return_field_name=None,
        required_fields=None,
        exclude_fields=None,
        only_fields=None,
        input_schema=None,
        registry=None,
        _meta=None,
        **kwargs,
    ):
        if not model:  # pragma: no cover
            raise ImproperlyConfigured("model is required for ModelMutation")
        if not _meta:
            _meta = ModelMutationOptions(cls)

        registry = registry or _registry
        exclude_fields = exclude_fields or []
        only_fields = only_fields or []
        if not return_field_name:
            return_field_name = _get_model_name(model)

        fdata = _get_fields(model, only_fields, exclude_fields, required_fields, registry)
        input_fields = yank_fields_from_attrs(
            {k: v["field"] for k, v in fdata.items()},
            _as=graphene.InputField,
        )

        input_schema = update_dict_nested(
            {k: v["schema"] for k, v in fdata.items()},
            input_schema or {},
        )

        fields = _get_output_fields(model, return_field_name, registry)

        _meta.model = model
        _meta.object_permissions = object_permissions or []
        _meta.object_permissions_any = object_permissions_any
        _meta.return_field_name = return_field_name
        _meta.exclude_fields = exclude_fields
        _meta.only_fields = only_fields
        _meta.required_fields = required_fields

        super().__init_subclass_with_meta__(
            _meta=_meta,
            input_fields=input_fields,
            input_schema=input_schema,
            registry=registry,
            **kwargs,
        )

        cls._meta.fields.update(fields)

    @classmethod
    def check_object_permissions(
        cls,
        info: ResolverInfo,
        instance: _T,
    ) -> bool:
        """Check object permissions for the given user.

        Subclasses can override this to avoid the permission checking or
        extending it. Remember to call `super()` in the later case.

        For this to work, the model needs to implement a `has_perm` method.
        The easiest way when using `guardian` is to inherit it
        from :class:`graphene_django_plus.models.GuardedModel`.

        """
        if not cls._meta.object_permissions:
            return True

        if not isinstance(instance, GuardedModel):
            return True

        return instance.has_perm(
            info.context.user,
            cls._meta.object_permissions,
            any_perm=cls._meta.object_permissions_any,
        )

    @classmethod
    def get_instance(cls, info: ResolverInfo, obj_id: str) -> _T:
        """Get an object given a relay global id."""
        instance = cls.get_node(info, obj_id)
        if not cls.check_object_permissions(info, instance):
            raise PermissionDenied()
        return cast(_T, instance)

    @classmethod
    def before_save(cls, info: ResolverInfo, instance: _T, cleaned_input: Dict[str, Any]):
        """Perform "before save" operations.

        Override this to perform any operation on the instance before
        its `.save()` method is called.

        """

    @classmethod
    def after_save(cls, info: ResolverInfo, instance: _T, cleaned_input: Dict[str, Any]):
        """Perform "after save" operations.

        Override this to perform any operation on the instance after its
        `.save()` method is called.

        """

    @classmethod
    def save(cls, info: ResolverInfo, instance: _T, cleaned_input: Dict[str, Any]):
        """Save the instance to the database.

        To do something with the instance "before" or "after" saving it,
        override either :meth:`.before_save` and/or :meth:`.after_save`.

        """
        cls.before_save(info, instance, cleaned_input=cleaned_input)
        instance.save()

        # save m2m and related object's data
        model = type(instance)
        for f in itertools.chain(
            model._meta.many_to_many,
            model._meta.related_objects,
            model._meta.private_fields,
        ):
            if isinstance(f, (ManyToOneRel, ManyToManyRel)):
                # Handle reverse side relationships.
                d = cleaned_input.get(f.related_name or f.name + "_set", None)
                if d is not None:
                    target_field = getattr(instance, f.related_name or f.name + "_set")
                    target_field.set(d)
            elif hasattr(f, "save_form_data"):
                d = cleaned_input.get(f.name, None)
                if d is not None:
                    f.save_form_data(instance, d)

        cls.after_save(info, instance, cleaned_input=cleaned_input)

    @classmethod
    def before_delete(cls, info: ResolverInfo, instance: _T):
        """Perform "before delete" operations.

        Override this to perform any operation on the instance before
        its `.delete()` method is called.

        """

    @classmethod
    def after_delete(cls, info: ResolverInfo, instance: _T):
        """Perform "after delete" operations.

        Override this to perform any operation on the instance after its
        `.delete()` method is called.

        """

    @classmethod
    def delete(cls, info: ResolverInfo, instance: _T):
        """Delete the instance from the database.

        To do something with the instance "before" or "after" deleting
        it, override either :meth:`.before_delete` and/or
        :meth:`.after_delete`.

        """
        cls.before_delete(info, instance)
        instance.delete()
        cls.after_delete(info, instance)


class ModelOperationMutation(BaseModelMutation[_T]):
    """Base mutation for operations on models.

    Just like a regular :class:`BaseModelMutation`, but this will
    receive only the object's id so an operation can happen to it.

    """

    class Meta:
        abstract = True

    @classmethod
    def __init_subclass_with_meta__(cls, **kwargs):
        super().__init_subclass_with_meta__(
            only_fields=["id"],
            required_fields=["id"],
            **kwargs,
        )


class ModelMutation(BaseModelMutation[_T]):
    """Create and update mutation for models.

    This will allow mutations for both create and update operations,
    depending on if the object's id is present in the input or not.

    """

    class Meta:
        abstract = True

    @classmethod
    def create_instance(cls, info: ResolverInfo, instance: _T, cleaned_data: Dict[str, Any]) -> _T:
        """Create a model instance given the already cleaned input data."""
        for f in type(instance)._meta.fields:
            if not f.editable or isinstance(f, models.AutoField) or f.name not in cleaned_data:
                continue

            data = cleaned_data[f.name]
            if data is None:
                # We want to reset the file field value when None was passed
                # in the input, but `FileField.save_form_data` ignores None
                # values. In that case we manually pass False which clears
                # the file.
                if isinstance(f, models.FileField):
                    data = False
                if not f.null:
                    data = f._get_default()  # type:ignore

            f.save_form_data(instance, data)

        return instance

    @classmethod
    def clean_instance(cls, info: ResolverInfo, instance: _T, clean_input: Dict[str, Any]) -> _T:
        """Validate the instance by calling its `.full_clean()` method."""
        try:
            instance.full_clean(exclude=cls._meta.exclude_fields)
        except ValidationError as e:
            if e.error_dict:
                raise e

        return instance

    @classmethod
    def clean_input(cls, info: ResolverInfo, instance: _T, data: Dict[str, Any]):
        """Clear and normalize the input data."""
        cleaned_input: Dict[str, Any] = {}

        for f_name, f_item in cls.Input._meta.fields.items():
            if f_name not in data:
                continue
            value = data[f_name]

            if value is not None and _is_list_of_ids(f_item):
                # list of IDs field
                instances = cls.get_nodes(info, value, f_name) if value else []
                cleaned_input[f_name] = instances
            elif value is not None and _is_id_field(f_item):
                # ID field
                instance = cls.get_node(info, value, f_name)
                cleaned_input[f_name] = instance
            elif value is not None and _is_upload_field(f_item):
                # uploaded files
                value = info.context.FILES.get(value)
                cleaned_input[f_name] = value
            else:
                # other fields
                cleaned_input[f_name] = value

        return cleaned_input

    @classmethod
    @transaction.atomic
    def perform_mutation(cls: Type[_MM], root, info: ResolverInfo, **data) -> _MM:
        """Perform the mutation.

        Create or update the instance, based on the existence of the
        `id` attribute in the input data and save it.

        """
        obj_id = data.get("id")
        if obj_id:
            checked_permissions = True
            instance = cls.get_instance(info, obj_id)
        else:
            checked_permissions = False
            instance = cls._meta.model()

        cleaned_input = cls.clean_input(info, instance, data)
        instance = cls.create_instance(info, instance, cleaned_input)
        instance = cls.clean_instance(info, instance, cleaned_input)
        cls.save(info, instance, cleaned_input)

        if not checked_permissions and not cls.check_object_permissions(info, instance):
            # If we did not check permissions when getting the instance,
            # check if here. The model might check the permissions based on
            # some related objects
            raise PermissionDenied()

        assert cls._meta.return_field_name
        return cls(**{cls._meta.return_field_name: instance})


class ModelCreateMutation(ModelMutation[_T]):
    """Create mutation for models.

    A shortcut for defining a :class:`ModelMutation` that already
    excludes the `id` from being required.

    """

    class Meta:
        abstract = True

    @classmethod
    def __init_subclass_with_meta__(cls, **kwargs):
        exclude_fields = kwargs.pop("exclude_fields", []) or []
        if "id" not in exclude_fields:
            exclude_fields.append("id")
        super().__init_subclass_with_meta__(
            exclude_fields=exclude_fields,
            **kwargs,
        )


class ModelUpdateMutation(ModelMutation[_T]):
    """Update mutation for models.

    A shortcut for defining a :class:`ModelMutation` that already
    enforces the `id` to be required.

    """

    class Meta:
        abstract = True

    @classmethod
    def __init_subclass_with_meta__(cls, **kwargs):
        if "only_fields" in kwargs and "id" not in kwargs["only_fields"]:
            kwargs["only_fields"].insert(0, "id")
        required_fields = kwargs.pop("required_fields", []) or []
        if "id" not in required_fields:
            required_fields.insert(0, "id")
        super().__init_subclass_with_meta__(
            required_fields=required_fields,
            **kwargs,
        )


class ModelDeleteMutation(ModelOperationMutation[_T]):
    """Delete mutation for models."""

    class Meta:
        abstract = True

    @classmethod
    @transaction.atomic
    def perform_mutation(cls: Type[_MM], root, info: ResolverInfo, **data) -> _MM:
        """Perform the mutation.

        Delete the instance from the database given its `id` attribute
        in the input data.

        """
        instance = cls.get_instance(info, data["id"])

        db_id = instance.id
        cls.delete(info, instance)

        # After the instance is deleted, set its ID to the original database's
        # ID so that the success response contains ID of the deleted object.
        instance.id = db_id

        assert cls._meta.return_field_name
        return cls(**{cls._meta.return_field_name: instance})
