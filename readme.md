# Anaya AI

Anaya is a modular desktop AI assistant built with explicit security, fine-grained command validation, and human-in-the-loop approvals for sensitive operations.

## Requirements

- Python `3.12` (`>=3.12, <3.13`)
- Windows PowerShell (or POSIX shell)

## Environment Setup

### Activate Virtual Environment (Windows PowerShell)

The existing preconfigured Python 3.12 virtual environment is `.venv312win`:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv312win\Scripts\Activate.ps1
```

### Dependency Management

Project dependencies are declared authoritatively in `pyproject.toml`.

The core application has **no mandatory third-party runtime dependencies** and runs entirely on the Python Standard Library.

#### Install Development Dependencies

To install the optional development and testing tools (such as `pytest`):

```powershell
pip install -e .[dev]
```

#### Optional Speech Dependencies

Optional speech libraries (`kokoro`, `soundfile`, `torch`) for future voice interfaces can be installed via:

```powershell
pip install -e .[speech]
```

*(Note: Speech and voice capabilities are planned for upcoming phases and are not yet implemented in the codebase.)*

## Running Tests

Run the test suite from the project root using `pytest`:

```powershell
pytest
```

Pytest is configured via `pyproject.toml` with `pythonpath = ["."]`, so the `app` package is resolved automatically without manual environment variable overrides.
