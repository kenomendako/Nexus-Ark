# Zhipu AI プロバイダ名取得キー修正

**日付:** 2026-02-05  
**ブランチ:** `fix/zhipu-provider-name-key`  
**ステータス:** ✅ 完了

---

## 問題の概要

グループ会話テスト中にZhipu AI (GLM-4.7-flash) モデルを使用すると、Error 1210（パラメータエラー）が発生していた。

```
Error code: 400 - {'error': {'code': '1210', 'message': 'API 调用参数有误，请检查文档。'}}
```

---

## 原因

`llm_factory.py` の行186で、ルーム個別のOpenAI設定からプロバイダ名を取得する際に、**キー名の不整合**があった：

| ファイル | 使用キー | 実際のキー |
|---------|---------|-----------|
| `llm_factory.py` | `"profile"` | `"name"` |
| `config_manager.py` | `"name"` | `"name"` |

この不整合により、`provider_name` がデフォルト値 `"Room-specific"` に設定され、Zhipu AI向けのパラメータ最適化（`temp=0.7`, `top_p=1.0`）が適用されなかった。

---

## 修正内容

### [llm_factory.py](file:///home/baken/nexus_ark/llm_factory.py#L186)

```diff
-provider_name = room_openai_settings.get("profile", "Room-specific")
+provider_name = room_openai_settings.get("name", "Room-specific")
```

---

## 検証結果

- [x] グループ会話でZhipu AI (glm-4.7-flash) を使用し、Error 1210が発生しないことを確認
- [x] ターミナルログで `Provider: Zhipu AI` と `[Optimization] Using recommended params...` が出力されることを確認
