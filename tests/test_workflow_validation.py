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


def test_validate_workflow_accepts_fallback_selectors_with_match() -> None:
    workflow = parse_workflow(
        """
version: 1
steps:
  - action: click
    selectors:
      - ".missing"
      - "button.primary"
    match: first_visible
"""
    )

    result = validate_workflow(workflow)

    assert result.ok is True


def test_validate_workflow_rejects_empty_selectors_list() -> None:
    workflow = parse_workflow(
        """
version: 1
steps:
  - action: wait_for
    selectors: []
"""
    )

    result = validate_workflow(workflow)

    assert result.ok is False
    assert any(issue.path == "steps[0].selectors" for issue in result.issues)


def test_validate_workflow_accepts_within_and_conditionals() -> None:
    workflow = parse_workflow(
        """
version: 1
steps:
  - action: if_exists
    selector: ".dialog"
    within:
      selector: "#container"
    then:
      - action: click
        selector: "button.confirm"
  - action: one_of
    branches:
      - name: on-cart
        url: "/cart"
        steps:
          - action: assert_url_contains
            url: "/cart"
      - name: fallback
        default: true
        steps:
          - action: wait_for_url_contains
            url: "/home"
"""
    )

    result = validate_workflow(workflow)

    assert result.ok is True


def test_validate_workflow_accepts_within_on_click_and_type() -> None:
    workflow = parse_workflow(
        """
version: 1
steps:
  - action: click
    selector: "button.confirm"
    within:
      selector: ".dialog"
  - action: type
    selector: "input[name='code']"
    text: "123456"
    within:
      selector: "form.verify"
"""
    )

    result = validate_workflow(workflow)

    assert result.ok is True


def test_validate_workflow_rejects_outputs_only_defined_in_one_branch() -> None:
    workflow = parse_workflow(
        """
version: 1
steps:
  - action: one_of
    branches:
      - name: cart
        url: "/cart"
        steps:
          - action: extract_text
            selector: "#cart-title"
            save_as: page_name
      - name: fallback
        default: true
        steps:
          - action: wait_for
            selector: "h1"
  - action: type
    selector: "#mirror"
    text: "${outputs.page_name}"
"""
    )

    result = validate_workflow(workflow)

    assert result.ok is False
    assert any(issue.path == "steps[1].text" for issue in result.issues)


def test_validate_workflow_accepts_outputs_defined_in_all_branches() -> None:
    workflow = parse_workflow(
        """
version: 1
steps:
  - action: one_of
    branches:
      - name: cart
        url: "/cart"
        steps:
          - action: extract_text
            selector: "#cart-title"
            save_as: page_name
      - name: home
        default: true
        steps:
          - action: extract_text
            selector: "h1"
            save_as: page_name
  - action: type
    selector: "#mirror"
    text: "${outputs.page_name}"
"""
    )

    result = validate_workflow(workflow)

    assert result.ok is True


def test_validate_workflow_rejects_if_exists_without_then_steps() -> None:
    workflow = parse_workflow(
        """
version: 1
steps:
  - action: if_exists
    selector: ".dialog"
"""
    )

    result = validate_workflow(workflow)

    assert result.ok is False
    assert any(issue.path == "steps[0].then" for issue in result.issues)
