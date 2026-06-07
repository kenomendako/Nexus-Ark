# arousal_calculator.py
"""
Arousal（感情的重要度）計算モジュール。
会話の内部状態変化からArousalスコアを計算する。

Arousalは会話の「感情的・知的インパクト」を0.0〜1.0で数値化したもの。
将来的にエピソード記憶の圧縮・検索での重み付けに使用する。
"""

from typing import Dict


def calculate_arousal(before: Dict, after: Dict) -> float:
    """
    内部状態の変化量からArousalスコアを計算する。
    
    Args:
        before: 会話開始時の内部状態スナップショット
        after: 会話終了時の内部状態スナップショット
        
    Returns:
        Arousalスコア（0.0〜1.0）
    """
    
    # --- 1. 好奇心の変化 = 知的興奮 ---
    curiosity_before = before.get("curiosity", 0.0)
    curiosity_after = after.get("curiosity", 0.0)
    curiosity_delta = abs(curiosity_after - curiosity_before)
    
    # --- 2. 関係性維持欲求の変化 = 深い関わり ---
    # 後方互換性: devotionがあればそれを使用、なければrelatednessを使用
    relatedness_before = before.get("relatedness", before.get("devotion", 0.0))
    relatedness_after = after.get("relatedness", after.get("devotion", 0.0))
    relatedness_delta = abs(relatedness_after - relatedness_before)
    
    # --- 3. ペルソナ感情の強度と変化 ---
    # カテゴリごとのArousal寄与ウェイト
    category_weights = {
        "joy": 0.8,          # 喜び = 高Arousal
        "anger": 1.0,        # 怒り = 最高Arousal
        "sadness": 0.6,      # 悲しみ = 中程度
        "protective": 0.5,   # 庇護欲 = 中程度
        "anxious": 0.5,      # 不安 = 中程度
        "contentment": 0.2,  # 満足 = 低Arousal
        "neutral": 0.0       # 平常 = なし
    }
    
    before_category = before.get("persona_emotion", "neutral")
    after_category = after.get("persona_emotion", "neutral")
    before_intensity = before.get("persona_intensity", 0.0)
    after_intensity = after.get("persona_intensity", 0.0)
    
    # ペルソナ感情からの寄与 = ウェイト × 強度
    before_contribution = category_weights.get(before_category, 0.0) * before_intensity
    after_contribution = category_weights.get(after_category, 0.0) * after_intensity
    
    # 変化と最大値の両方を考慮
    emotion_delta = abs(after_contribution - before_contribution)
    emotion_max = max(before_contribution, after_contribution)
    
    # 強い感情があれば最低限の重要度を保証
    if emotion_max >= 0.5:
        emotion_delta = max(emotion_delta, 0.3)
    
    # --- 4. 複合Arousalスコア ---
    raw_arousal = (
        curiosity_delta * 0.25 +
        emotion_delta * 0.40 +
        emotion_max * 0.15 +        # 絶対的な感情の強さも考慮
        relatedness_delta * 0.20
    )
    
    # スケール調整（変化が小さくてもある程度のスコアが出るように）
    arousal = min(1.0, raw_arousal * 2.0)
    
    return round(arousal, 3)


def get_arousal_level(score: float) -> str:
    """
    Arousalスコアを人間が読みやすいレベルに変換する。
    
    Args:
        score: Arousalスコア（0.0〜1.0）
        
    Returns:
        レベル文字列（low, medium, high, very_high）
    """
    if score < 0.25:
        return "low"
    elif score < 0.5:
        return "medium"
    elif score < 0.75:
        return "high"
    else:
        return "very_high"
