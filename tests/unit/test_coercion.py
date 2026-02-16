from codex_autorunner.core.coercion import coerce_int


class TestCoerceIntNone:
    def test_none_returns_default(self):
        assert coerce_int(None) is None

    def test_none_with_custom_default(self):
        assert coerce_int(None, default=42) == 42

    def test_none_with_zero_default(self):
        assert coerce_int(None, default=0) == 0


class TestCoerceIntIntValues:
    def test_positive_int(self):
        assert coerce_int(123) == 123

    def test_negative_int(self):
        assert coerce_int(-456) == -456

    def test_zero(self):
        assert coerce_int(0) == 0

    def test_large_int(self):
        assert coerce_int(10**18) == 10**18


class TestCoerceIntFloatValues:
    def test_float_truncates(self):
        assert coerce_int(3.7) == 3

    def test_float_negative_truncates(self):
        assert coerce_int(-2.9) == -2

    def test_float_whole_number(self):
        assert coerce_int(5.0) == 5

    def test_float_zero(self):
        assert coerce_int(0.0) == 0

    def test_float_inf_returns_default(self):
        assert coerce_int(float("inf")) is None

    def test_float_neg_inf_returns_default(self):
        assert coerce_int(float("-inf")) is None

    def test_float_nan_returns_default(self):
        assert coerce_int(float("nan")) is None

    def test_float_inf_with_custom_default(self):
        assert coerce_int(float("inf"), default=-1) == -1


class TestCoerceIntStringValues:
    def test_string_int(self):
        assert coerce_int("42") == 42

    def test_string_negative_int(self):
        assert coerce_int("-17") == -17

    def test_string_zero(self):
        assert coerce_int("0") == 0

    def test_string_float_converts(self):
        assert coerce_int("3.14") == 3

    def test_string_negative_float_converts(self):
        assert coerce_int("-2.7") == -2

    def test_string_with_whitespace_accepted(self):
        assert coerce_int(" 42 ") == 42

    def test_string_invalid_returns_default(self):
        assert coerce_int("not a number") is None

    def test_string_invalid_with_custom_default(self):
        assert coerce_int("abc", default=99) == 99

    def test_string_empty_returns_default(self):
        assert coerce_int("") is None

    def test_string_hex_not_supported(self):
        assert coerce_int("0x10") is None

    def test_string_scientific_notation(self):
        assert coerce_int("1e3") == 1000


class TestCoerceIntBool:
    def test_bool_true_rejected_by_default(self):
        assert coerce_int(True) is None

    def test_bool_false_rejected_by_default(self):
        assert coerce_int(False) is None

    def test_bool_true_with_reject_bool_false(self):
        assert coerce_int(True, reject_bool=False) == 1

    def test_bool_false_with_reject_bool_false(self):
        assert coerce_int(False, reject_bool=False) == 0

    def test_bool_true_with_custom_default_rejected_returns_default(self):
        assert coerce_int(True, default=5) == 5

    def test_bool_true_with_custom_default_accepted(self):
        assert coerce_int(True, default=5, reject_bool=False) == 1


class TestCoerceIntEdgeCases:
    def test_list_returns_default(self):
        assert coerce_int([1, 2, 3]) is None

    def test_dict_returns_default(self):
        assert coerce_int({"a": 1}) is None

    def test_tuple_returns_default(self):
        assert coerce_int((1,)) is None

    def test_none_with_none_default(self):
        assert coerce_int(None, default=None) is None

    def test_very_large_int(self):
        big_int = 2**63 - 1
        assert coerce_int(big_int) == big_int

    def test_very_negative_int(self):
        neg_int = -(2**63)
        assert coerce_int(neg_int) == neg_int

    def test_large_float_truncates(self):
        assert coerce_int(1e15) == int(1e15)
