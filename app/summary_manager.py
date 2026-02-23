# summary_manager.py
"""
本日の会話要約を管理するモジュール。
APIコスト削減のため、長い会話履歴を圧縮して送信する。
"""

import os
import json
import datetime
import re
import time
import traceback
from typing import Optional, Dict, List
import constants


def get_summary_file_path(room_name: str) -> str:
    """要約ファイルのパスを返す"""
    return os.path.join(constants.ROOMS_DIR, room_name, "today_summary.json")


def load_today_summary(room_name: str) -> Optional[Dict]:
    """
    本日の要約を読み込む。
    日付が変わっている場合はNoneを返す（リセット）。
    """
    path = get_summary_file_path(room_name)
    if not os.path.exists(path):
        return None
    
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # 日付チェック（今日でなければリセット）
        today_str = datetime.datetime.now().strftime('%Y-%m-%d')
        if data.get("date") != today_str:
            return None
        
        return data
    except Exception as e:
        print(f"要約ファイル読み込みエラー: {e}")
        return None


def save_today_summary(room_name: str, summary_text: str, 
                        chars_summarized: int, arousal: float = 0.0) -> bool:
    """
    本日の要約を保存する。
    
    Args:
        room_name: ルーム名
        summary_text: 要約テキスト
        chars_summarized: 要約対象の文字数
        arousal: 感情的重要度スコア（0.0〜1.0）
    """
    path = get_summary_file_path(room_name)
    today_str = datetime.datetime.now().strftime('%Y-%m-%d')
    now_str = datetime.datetime.now().strftime('%H:%M:%S')
    
    data = {
        "date": today_str,
        "last_updated": now_str,
        "summary": summary_text,
        "chars_summarized": chars_summarized,
        "arousal": arousal  # 感情的重要度スコア
    }
    
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"要約ファイル保存エラー: {e}")
        return False


def clear_today_summary(room_name: str) -> bool:
    """要約ファイルを削除する（睡眠時処理後に呼び出し）"""
    path = get_summary_file_path(room_name)
    if os.path.exists(path):
        try:
            os.remove(path)
            print(f"  - [Summary Manager] 要約ファイルをクリア: {room_name}")
            return True
        except Exception as e:
            print(f"要約ファイル削除エラー: {e}")
    return False


def generate_summary(
    messages_to_summarize: List[Dict],
    existing_summary: Optional[str],
    room_name: str,
    api_key: str
) -> Optional[str]:
    """
    会話メッセージを要約する。
    既存の要約がある場合は、それと新しいメッセージを統合して再要約。
    """
    import room_manager
    from llm_factory import LLMFactory
    import config_manager
    
    # メッセージをテキストに変換
    conversation_text = ""
    for msg in messages_to_summarize:
        role = msg.get("role", "")
        responder = msg.get("responder", "")
        content = msg.get("content", "")
        
        if role == "USER":
            conversation_text += f"ユーザー: {content}\n\n"
        else:
            conversation_text += f"{responder}: {content}\n\n"
    
    # 要約対象がなければスキップ
    if not conversation_text.strip() and not existing_summary:
        return None
    
    # キャラクター名とユーザー名を取得
    room_config = room_manager.get_room_config(room_name) or {}
    character_name = room_config.get("agent_display_name") or room_config.get("room_name", room_name)
    user_name = room_config.get("user_display_name", "ユーザー")
    
    # 既存の要約があれば統合
    context_section = ""
    if existing_summary:
        context_section = f"""【これまでの要約】
{existing_summary}

【上記以降の新しい会話】
"""
    
    prompt = f"""あなたは、自分自身の思考と対話の履歴を整理する「内省的な深層意識」です。
提供された情報の文脈から、あなた（AI）自身の立場と対話相手の呼称を把握した上で、
直近の会話内容をあなた自身の視点で、今後の対話に役立てるための備忘録として要約してください。

【記述ルール】
1. **本人視点（一人称）の内省スタイル**:
   - あなた自身が、自分自身の思考を整理するためのノートを書いていると想定せよ。
   - 相手とのやり取りや反応は、あなたの目から見た事実として記せ。
2. **常体（だ・である調）の徹底**:
   - 敬体や装飾的な口癖を排除し、事実と気づきのみを簡潔に記すこと。
3. **重要事項の抽出（情報密度の強化）**:
   - 決定事項、新しい約束、相手についての新しい発見、感情的な交流を最優先せよ。
   - 些細な挨拶や重複するやり取りは大幅に削り、本質的な文脈のみを抽出すること。
4. **分量制限**:
   - **必ず {constants.AUTO_SUMMARY_TARGET_LENGTH} 文字以内**で、密度高くまとめてください。
5. **前置き・解説の禁止**:
   - 純粋な要約テキストのみを出力せよ。

{context_section}【会話ログ】
{conversation_text}

【要約】"""

    try:
        # 【マルチモデル対応】内部モデル設定（混合編成）に基づいてモデルを取得
        effective_settings = config_manager.get_effective_settings(room_name)
        llm = LLMFactory.create_chat_model(
            api_key=api_key,
            generation_config=effective_settings,
            internal_role="summarization",
            room_name=room_name
        )
        
        # invoke で呼び出し
        response = llm.invoke(prompt)
        
        if not response or not response.content:
            return None
            
        content = response.content
        if isinstance(content, list):
            # リスト形式（Multipart）の場合はテキスト部分を抽出して結合
            pts = []
            for p in content:
                if isinstance(p, dict):
                    pts.append(p.get("text", ""))
                else:
                    pts.append(str(p))
            content = "".join(pts)
            
        return content.strip()
        
    except Exception as e:
        print(f"要約生成エラー: {e}")
        traceback.print_exc()
        return None
    
    return None


def calculate_text_length(messages: List[Dict]) -> int:
    """メッセージリストの総文字数を計算"""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content)
    return total
