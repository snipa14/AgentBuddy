"""Loads and validates skill eval fixture cases from a cases.yaml file."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class EvalCase(BaseModel):
    """One fixture case for a skill's eval: an input plus what must show up in the response.

    Attributes:
        name: A short, unique identifier for this case (used in PASS/FAIL output).
        input: The user-prompt text sent to the model alongside the skill's body.
        must_match_any: Case-insensitive regex patterns; the case passes if the
            model's response matches at least one.
        image_path: Resolved absolute path to an image file to send alongside
            `input`, or None for a text-only case.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    input: str
    must_match_any: list[str]
    image_path: Path | None = None


class InvalidCaseFileError(ValueError):
    """Raised when a cases.yaml file is missing required fields or malformed."""


class _RawExpect(BaseModel):
    """The `expect:` block of one raw, not-yet-resolved cases.yaml entry."""

    must_match_any: list[str] = Field(default_factory=list)


class _RawCaseEntry(BaseModel):
    """One raw, not-yet-resolved cases.yaml entry.

    `image` stays a plain relative string here -- `load_cases()` resolves it
    against `cases_path`'s parent and checks it exists, which needs context
    this model doesn't have.
    """

    name: str
    input: str
    expect: _RawExpect
    image: str | None = None


def load_cases(cases_path: Path) -> list[EvalCase]:
    """Loads and validates eval cases from a cases.yaml file.

    Args:
        cases_path: Path to a skill's eval/cases.yaml file.

    Returns:
        The EvalCase objects declared under the file's `cases:` key, in file
        order. An `image` key, if present on a case, is resolved relative to
        cases_path's parent directory.

    Raises:
        InvalidCaseFileError: If the file has no `cases:` key, a case entry
            isn't a mapping, a case is missing `name`/`input`/`expect`,
            `expect` isn't a mapping matching its schema (e.g. a plain
            string, or a `must_match_any` that isn't a list of strings),
            `expect.must_match_any` is empty, or a referenced `image` file
            doesn't exist.

    Note:
        `yaml` is imported locally here, not at module level, so that
        importing `EvalCase`/`InvalidCaseFileError` never requires pyyaml
        to be installed -- only calling `load_cases()` itself does.
    """
    import yaml

    raw = yaml.safe_load(cases_path.read_text())
    if not raw or "cases" not in raw:
        raise InvalidCaseFileError(f"{cases_path}: missing top-level 'cases:' key")

    cases: list[EvalCase] = []
    for i, entry in enumerate(raw["cases"]):
        if not isinstance(entry, dict):
            raise InvalidCaseFileError(
                f"{cases_path}: case {i} must be a mapping, got {type(entry).__name__}"
            )

        missing = [key for key in ("name", "input", "expect") if key not in entry]
        if missing:
            raise InvalidCaseFileError(f"{cases_path}: case {i} missing keys {missing}")

        try:
            parsed = _RawCaseEntry.model_validate(entry)
        except ValidationError as exc:
            raise InvalidCaseFileError(
                f"{cases_path}: case {i} ('{entry['name']}') is malformed: {exc}"
            ) from exc

        if not parsed.expect.must_match_any:
            raise InvalidCaseFileError(
                f"{cases_path}: case {i} ('{parsed.name}') has an empty must_match_any"
            )

        image_path = None
        if parsed.image is not None:
            image_path = (cases_path.parent / parsed.image).resolve()
            if not image_path.is_file():
                raise InvalidCaseFileError(
                    f"{cases_path}: case {i} ('{parsed.name}') references "
                    f"missing image file {image_path}"
                )

        cases.append(
            EvalCase(
                name=parsed.name,
                input=parsed.input,
                must_match_any=parsed.expect.must_match_any,
                image_path=image_path,
            )
        )
    return cases
