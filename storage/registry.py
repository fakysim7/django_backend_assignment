from django.conf import settings

from .providers import (
    LocalProvider,
    StorageConfigurationError,
    StorageError,
    TigrisProvider,
)

_PROVIDERS = {"local": LocalProvider, "tigris": TigrisProvider}
_instances = {}


def register_provider(name, provider_class):
    if not name or not isinstance(name, str):
        raise ValueError("Provider name must be a non-empty string.")
    _PROVIDERS[name] = provider_class
    _instances.pop(name, None)


def get_provider(name=None):
    selected = name or settings.STORAGE_PROVIDER
    if selected not in _PROVIDERS:
        raise StorageConfigurationError("Configured storage provider is unknown.")
    if selected not in _instances:
        try:
            _instances[selected] = _PROVIDERS[selected]()
        except StorageConfigurationError:
            if (
                selected == "tigris"
                and settings.STORAGE_ALLOW_LOCAL_FALLBACK
                and name is None
                and (
                    not settings.TIGRIS_ACCESS_KEY_ID
                    or not settings.TIGRIS_SECRET_ACCESS_KEY
                )
            ):
                _instances["local"] = _instances.get("local") or LocalProvider()
                return _instances["local"]
            raise
        except OSError as error:
            raise StorageError("Configured storage provider could not initialize.") from error
    return _instances[selected]


def provider_status():
    selected = settings.STORAGE_PROVIDER
    try:
        provider = get_provider()
    except StorageError:
        return {
            "configured_provider": selected,
            "active_provider": None,
            "configuration_ready": False,
            "bucket": settings.TIGRIS_BUCKET if selected == "tigris" else "",
        }
    return {
        "configured_provider": selected,
        "active_provider": provider.name,
        "configuration_ready": True,
        "bucket": getattr(provider, "bucket", ""),
    }


def reset_provider_registry():
    _instances.clear()
