"""感情の参考値の時間条件。版1の旧記録は常に標準6時間/24時間で読む。"""
import math


class EmotionDecayPolicy:
    VERSION = 1
    MIN_HOURS = 0.25
    MAX_HOURS = 168.0
    FIELDS = {"version", "half_life_hours", "max_age_hours"}

    @staticmethod
    def default():
        # 条件のない旧記録にも使う固定値。現在のルーム設定は参照しない。
        return {"version": 1, "half_life_hours": 6.0, "max_age_hours": 24.0}

    @classmethod
    def validate(cls, raw):
        """不足・未知の条件を補わず、対応する完全な条件だけを受け入れる。"""
        if (not isinstance(raw, dict) or set(raw) != cls.FIELDS
                or type(raw.get("version")) is not int or raw["version"] != cls.VERSION):
            raise ValueError("時間条件はversion=1と半減期・最長提示期間の完全な設定で指定してください。")
        policy = {"version": cls.VERSION}
        for key in ("half_life_hours", "max_age_hours"):
            value = raw[key]
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError("時間条件は0.25〜168時間の有限な数値で指定してください。")
            try:
                value = float(value)
            except (ValueError, OverflowError):
                raise ValueError("時間条件は0.25〜168時間の有限な数値で指定してください。") from None
            if not math.isfinite(value) or not cls.MIN_HOURS <= value <= cls.MAX_HOURS:
                raise ValueError("時間条件は0.25〜168時間の有限な数値で指定してください。")
            policy[key] = value
        return policy
