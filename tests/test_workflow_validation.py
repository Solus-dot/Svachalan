from svachalan import parse_workflow, validate_workflow


def test_validate_workflow_rejects_duplicate_save_as() -> None:
    workflow = parse_workflow(
        """
version: 1
steps:
  - action: extract_text
    selector: ".one"
    save_as: balance
  - action: extract_text
    selector: ".two"
    save_as: balance
"""
    )

    result = validate_workflow(workflow)

    assert result.ok is False
    assert any(issue.path == "steps[1].save_as" for issue in result.issues)


def test_validate_workflow_rejects_retry_on_side_effecting_action() -> None:
    workflow = parse_workflow(
        """
version: 1
steps:
  - action: click
    selector: "button"
    retry_count: 1
"""
    )

    result = validate_workflow(workflow)

    assert result.ok is False
    assert any(issue.path == "steps[0].retry_count" for issue in result.issues)


def test_validate_workflow_rejects_unsupported_namespace() -> None:
    workflow = parse_workflow(
        """
version: 1
steps:
  - action: type
    selector: "#email"
    text: "${env.EMAIL}"
"""
    )

    result = validate_workflow(workflow)

    assert result.ok is False
    assert any("Unsupported interpolation namespace" in issue.message for issue in result.issues)


def test_validate_workflow_rejects_forward_output_reference() -> None:
    workflow = parse_workflow(
        """
version: 1
steps:
  - action: type
    selector: "#mirror"
    text: "${outputs.balance}"
  - action: extract_text
    selector: ".balance"
    save_as: balance
"""
    )

    result = validate_workflow(workflow)

    assert result.ok is False
    assert any(
        issue.path == "steps[0].text" and "previous step" in issue.message
        for issue in result.issues
    )
