"""Validate the persisted plugin settings shared by tools and the dashboard."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    interval: int = 100
    max_pages: int = 100
    memory_bytes: int = 8000
    catalog_bytes: int = 20000

    @classmethod
    def read(cls, config):
        """Reject invalid settings rather than treating them as unlimited."""
        fields = {
            "memory_reminder_interval": ("interval", 100),
            "notebook_max_pages": ("max_pages", 100),
            "long_term_memory_max_bytes": ("memory_bytes", 8000),
            "notebook_catalog_max_bytes": ("catalog_bytes", 20000),
        }
        enabled = config.get("memory_reminder_enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("invalid_settings")
        values = {"enabled": enabled}
        for key, (field, default) in fields.items():
            value = config.get(key, default)
            if type(value) is not int or value < 1:
                raise ValueError("invalid_settings")
            values[field] = value
        return cls(**values)


CONFIG_KEYS = (
    "memory_reminder_enabled",
    "memory_reminder_interval",
    "notebook_max_pages",
    "long_term_memory_max_bytes",
    "notebook_catalog_max_bytes",
)
