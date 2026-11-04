# Development and maintenance tools

Run these from the repository root with its Python environment. The robot starts
through `robbi`; these scripts are not separate robot services.

| Folder | Purpose |
| --- | --- |
| `apps/` | Validate and package standalone projects for Robbi App Hub. |
| `accounts/` | Authorize optional Google integrations on a computer with a browser. |
| `assets/` | Optional artwork and soundtrack authoring recipes; use on a development computer. |
| `hardware/` | Pi preflight checks, explicit board diagnostics and the motor-disable service hook. |
| `release/` | Review, seal and package the public distribution locally. No upload. |

`hardware/motor_disable.py` is required by the startup service; it is not an
obsolete legacy export. Hardware diagnostics require stopping the robot service
to avoid competing owners of the same GPIO pins.

The Animation Agent Kit is not included. Personal export scripts, migration
experiments, private filming sequences and the Hub website do not belong here.
