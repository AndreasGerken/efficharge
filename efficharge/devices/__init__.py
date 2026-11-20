"""Device adapters. Importing this package registers all of them (see ``base.register``)."""

from efficharge.devices import meross, simulated  # noqa: F401  (registration side effect)
