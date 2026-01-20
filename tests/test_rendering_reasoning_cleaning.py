from codex_autorunner.integrations.telegram.rendering import (
    _clean_reasoning_from_output,
)


def test_clean_reasoning_from_output_no_reasoning():
    text = """Here is the code:

```python
def hello():
    print("world")
```

This is a simple function."""
    result = _clean_reasoning_from_output(text)
    assert result == text


def test_clean_reasoning_from_output_with_reasoning_prefix():
    text = """[reasoning] I need to create a function
thinking: Let me write the code

Here is the code:

```python
def hello():
    print("world")
```"""
    expected = """Here is the code:

```python
def hello():
    print("world")
```"""
    result = _clean_reasoning_from_output(text)
    assert result == expected


def test_clean_reasoning_from_output_with_reasoning_in_code_block():
    text = """```python
def hello():
    # [reasoning] This is a comment, should be kept
    print("world")
```"""
    result = _clean_reasoning_from_output(text)
    assert result == text


def test_clean_reasoning_from_output_mixed():
    text = """thinking: I should write a function

Here is my response:

```python
def hello():
    print("world")
```

thought: I'm done"""
    expected = """Here is my response:

```python
def hello():
    print("world")
```"""
    result = _clean_reasoning_from_output(text)
    assert result == expected


def test_clean_reasoning_from_output_case_insensitive():
    text = """REASONING: Let me think
THINKING: About the code
[reasoning] Another thought

Here is the answer."""
    expected = """Here is the answer."""
    result = _clean_reasoning_from_output(text)
    assert result == expected


def test_clean_reasoning_from_output_preserves_normal_lines():
    text = """First line
Second line
Third line"""
    result = _clean_reasoning_from_output(text)
    assert result == text


def test_clean_reasoning_from_output_empty():
    text = ""
    result = _clean_reasoning_from_output(text)
    assert result == ""
