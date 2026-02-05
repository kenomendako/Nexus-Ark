# llm_factory.py

import os
from langchain_google_genai import ChatGoogleGenerativeAI, HarmBlockThreshold, HarmCategory
from langchain_openai import ChatOpenAI
import config_manager
import utils
import gemini_api # 既存のGemini設定ロジックを再利用するため

class LLMFactory:
    @staticmethod
    def create_chat_model(
        model_name: str = None,
        temperature: float = 0.7,
        top_p: float = 0.95,
        max_retries: int = 0,
        api_key: str = None, # Gemini用 (OpenAI系は設定から取得)
        generation_config: dict = None,
        force_google: bool = False,  # 内部処理用にGeminiを強制使用する場合True
        room_name: str = None,  # ルーム名（ルーム個別のプロバイダ設定を取得するため）
        internal_role: str = None  # [Phase 2] "processing", "summarization", "supervisor"
    ):
        """
        現在の設定(active_provider)に基づいて、適切なLangChain ChatModelインスタンスを生成して返す。
        
        Args:
            model_name: 使用するモデル名（internal_role指定時は省略可）
            temperature: 生成温度
            top_p: Top-P
            max_retries: リトライ回数（Agent側で制御するため基本0）
            api_key: Geminiを使用する場合のAPIキー
            generation_config: その他の生成設定（安全性設定など）
            force_google: Trueの場合、active_providerに関係なくGemini Nativeを使用。
                          内部処理（検索クエリ生成、情景描写等）はGemini固定のため。
                          ※internal_role指定時は無視される（後方互換用）
            room_name: ルーム名。指定するとルーム個別のプロバイダ設定を優先する。
            internal_role: [Phase 2] 内部処理のロール。"processing", "summarization", "supervisor"のいずれか。
                          指定すると、config.jsonの内部モデル設定に基づいてプロバイダとモデルを自動選択。
        """
        config_manager.load_config() 
        
        # --- [Phase 2] internal_role優先ロジック ---
        if internal_role:
            # 1. プロバイダとモデル名の決定
            active_provider, effective_model_name = config_manager.get_effective_internal_model(internal_role)
            print(f"--- [LLM Factory] 内部モデル設定（混合編成）適用: {internal_role} ---")
            print(f"  - Provider: {active_provider}")
            print(f"  - Model: {effective_model_name}")
            
            # 2. モデル名のサニタイズ
            sanitized_model_name = utils.sanitize_model_name(effective_model_name or "")

            # 3. プロバイダごとの分岐処理
            if active_provider == "google":
                if not api_key:
                    # [2026-02-01 FIX] 内部処理(internal_role)ではルーム個別設定ではなく共通設定のAPIキーを優先する
                    api_key = config_manager.get_active_gemini_api_key(None)
                if not api_key:
                    raise ValueError("Google provider requires an API key. No valid key found.")
                return gemini_api.get_configured_llm(
                    model_name=sanitized_model_name,
                    api_key=api_key,
                    generation_config=generation_config or {}
                )
            elif active_provider == "local":
                local_model_path = config_manager.LOCAL_MODEL_PATH
                if not local_model_path or not os.path.exists(local_model_path):
                    raise ValueError(f"Local LLM requires a valid GGUF model path. Current: '{local_model_path}'")
                try:
                    from langchain_community.chat_models import ChatLlamaCpp
                    return ChatLlamaCpp(
                        model_path=local_model_path,
                        temperature=temperature,
                        n_ctx=4096,
                        n_gpu_layers=0,
                        verbose=False
                    )
                except ImportError:
                    raise ValueError("llama-cpp-python is not installed.")
            else:
                # OpenAI互換としての処理
                openai_setting = config_manager.get_openai_setting_by_name(active_provider)
                # レガシーキーワード対応
                if not openai_setting:
                    if active_provider == "zhipu":
                        openai_setting = {"base_url": "https://open.bigmodel.cn/api/paas/v4/", "api_key": config_manager.ZHIPU_API_KEY, "name": "Zhipu AI"}
                    elif active_provider == "groq":
                        openai_setting = {"base_url": "https://api.groq.com/openai/v1", "api_key": config_manager.GROQ_API_KEY, "name": "Groq"}
                    elif active_provider == "moonshot":
                        openai_setting = {"base_url": "https://api.moonshot.cn/v1", "api_key": config_manager.MOONSHOT_API_KEY, "name": "Moonshot AI"}
                
                if not openai_setting:
                    # アクティブなプロファイルをフォールバックとして試行
                    openai_setting = config_manager.get_active_openai_setting()
                
                if not openai_setting:
                    raise ValueError(f"Unsupported internal model provider: {active_provider}")

                base_url = openai_setting.get("base_url")
                openai_api_key = openai_setting.get("api_key", "dummy")
                provider_name = openai_setting.get("name", active_provider)

                # パラメータ最適化
                target_temp = temperature
                target_top_p = top_p
                if provider_name == "Zhipu AI" or "glm" in sanitized_model_name.lower():
                    if "glm-4.7-flash" in sanitized_model_name.lower():
                        target_temp = 0.7 if temperature == 1.0 or temperature == 0.7 else temperature
                        target_top_p = 1.0 if top_p == 0.95 or top_p == 1.0 else top_p
                elif provider_name == "Moonshot AI" or "moonshot" in base_url:
                    if target_temp != 1.0: target_temp = 1.0

                return ChatOpenAI(
                    base_url=base_url,
                    api_key=openai_api_key,
                    model=sanitized_model_name,
                    temperature=target_temp,
                    top_p=target_top_p,
                    max_retries=max_retries,
                    streaming=True
                )
        
        # --- 以下は既存ロジック（internal_role未指定時） ---
        
        # モデル名から注釈やお気に入りマークを除去するサニタイズ処理
        # 例: "⭐ glm-4.7-flash (Recommended)" -> "glm-4.7-flash"
        internal_model_name = utils.sanitize_model_name(model_name)

        # ルーム名を渡してルーム個別のプロバイダ設定を優先する
        active_provider = config_manager.get_active_provider(room_name)
        
        # 【マルチモデル対応】内部処理用モデルは強制的にGemini APIを使用
        # ユーザー設定のプロバイダ（OpenAI等）に関係なく、Gemini固定が必要な処理用
        if force_google:
            print(f"--- [LLM Factory] Force Google mode (Legacy): Using Gemini Native ---")
            print(f"  - Model: {internal_model_name}")
            active_provider = "google"

        # --- Google Gemini (Native) ---
        if active_provider == "google":
            key_name_for_log = "Unknown"
            
            # api_key が未指定なら自動補完
            if not api_key:
                api_key = config_manager.get_active_gemini_api_key(room_name)
                # 設定から取得した場合、そのコンテキストでの名前も取得
                key_name = config_manager.get_active_gemini_api_key_name(room_name)
                if key_name:
                    key_name_for_log = key_name
            else:
                # 既にAPIキーが渡されている場合、値から逆引きして名前を特定
                key_name_for_log = config_manager.get_key_name_by_value(api_key)

            if not api_key:
                raise ValueError("Google provider requires an API key. No valid key found.")

            # マスクされたキーを作成 (例: AIza...5678)
            masked_key = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else "***"
            
            print(f"--- [LLM Factory] Initializing Gemini Model ---")
            print(f"  - Model: {internal_model_name}")
            print(f"  - API Key: {key_name_for_log} ({masked_key})")
                
            return gemini_api.get_configured_llm(
                model_name=internal_model_name,
                api_key=api_key,
                generation_config=generation_config
            )

        # --- (LEGACY) Zhipu AI (GLM-4) branch removed ---
        # Now handled by "openai" branch below


        # --- OpenAI Compatible (OpenRouter, Groq, Ollama, etc.) ---
        elif active_provider == "openai":
            # ルーム個別のOpenAI設定を優先
            # generation_configにopenai_settingsが含まれていればそれを使用
            room_openai_settings = None
            if generation_config and isinstance(generation_config, dict):
                room_openai_settings = generation_config.get("openai_settings")
            
            if room_openai_settings and room_openai_settings.get("base_url"):
                # ルーム個別設定を使用
                base_url = room_openai_settings.get("base_url")
                openai_api_key = room_openai_settings.get("api_key", "dummy")
                provider_name = room_openai_settings.get("name", "Room-specific")
                print(f"--- [LLM Factory] Using room-specific OpenAI settings ---")
            else:
                # フォールバック: グローバルなアクティブプロファイルの設定を取得
                openai_setting = config_manager.get_active_openai_setting()
                if not openai_setting:
                    raise ValueError("No active OpenAI provider profile found.")
                base_url = openai_setting.get("base_url")
                openai_api_key = openai_setting.get("api_key")
                provider_name = openai_setting.get("name")
            
            # OllamaなどはAPIキーが不要な場合があるが、ライブラリの仕様上ダミーが必要なことがある
            if not openai_api_key:
                openai_api_key = "dummy"

            # generation_config から OpenAI がサポートするパラメータのみを抽出する（ホワイトリスト方式）
            model_kwargs = {}
            openai_whitelist = [
                "presence_penalty", "frequency_penalty", "logit_bias", "user",
                "response_format", "seed", "stop", "n"
            ]
            max_tokens = None
            
            if generation_config and isinstance(generation_config, dict):
                for k, v in generation_config.items():
                    if k in openai_whitelist:
                        model_kwargs[k] = v
                
                if "max_tokens" in generation_config:
                    max_tokens = generation_config["max_tokens"]

            # Zhipu AI 向けの最適化
            target_temp = temperature
            target_top_p = top_p
            if provider_name == "Zhipu AI":
                if "glm-4.7-flash" in internal_model_name.lower():
                    # 智譜AI推奨値: temp=0.7, top_p=1.0
                    target_temp = 0.7 if temperature == 1.0 or temperature == 0.7 else temperature
                    target_top_p = 1.0 if top_p == 0.95 or top_p == 1.0 else top_p
                    print(f"  - [Optimization] Using recommended params for {internal_model_name}: temp={target_temp}, top_p={target_top_p}")
            elif provider_name == "Moonshot AI" or "moonshot" in base_url:
                # Moonshot AI (Kimi) は temperature=1.0 以外を受け付けない場合がある
                # Error: "invalid temperature: only 1 is allowed for this model"
                if target_temp != 1.0:
                    print(f"  - [Override] Moonshot AI requires temperature=1.0 (was {target_temp}). Adjusting.")
                    target_temp = 1.0

            print(f"--- [LLM Factory] Creating OpenAI-compatible client ---")
            print(f"  - Provider: {provider_name}")
            print(f"  - Base URL: {base_url}")
            print(f"  - Model: {internal_model_name}")

            return ChatOpenAI(
                base_url=base_url,
                api_key=openai_api_key,
                model=internal_model_name,
                temperature=target_temp,
                top_p=target_top_p,
                max_tokens=max_tokens,
                max_retries=max_retries,
                streaming=True,
                model_kwargs=model_kwargs
            )

        else:
            raise ValueError(f"Unknown provider: {active_provider}")

    @staticmethod
    def create_chat_model_with_fallback(
        internal_role: str,
        room_name: str = None,
        temperature: float = 0.7,
        **kwargs
    ):
        """
        [Phase 4] フォールバック機構付きでチャットモデルを生成する。
        
        プライマリプロバイダでエラーが発生した場合、設定されたフォールバック順序に従って
        次のプロバイダを試行する。
        
        Args:
            internal_role: "processing", "summarization", "supervisor" のいずれか
            room_name: ルーム名
            temperature: 生成温度
            **kwargs: その他のオプション
            
        Returns:
            LangChain ChatModel インスタンス
            
        Raises:
            ValueError: すべてのプロバイダで失敗した場合
        """
        settings = config_manager.get_internal_model_settings()
        primary_provider = settings.get("provider", "google")
        fallback_enabled = settings.get("fallback_enabled", True)
        fallback_order = settings.get("fallback_order", ["google"])
        
        # 試行するプロバイダリストを構築（プライマリ + フォールバック順）
        providers_to_try = [primary_provider]
        if fallback_enabled:
            for fb_provider in fallback_order:
                if fb_provider != primary_provider and fb_provider not in providers_to_try:
                    providers_to_try.append(fb_provider)
        
        errors = []
        for provider in providers_to_try:
            try:
                # プロバイダを一時的に上書きして試行
                original_provider = settings.get("provider")
                settings["provider"] = provider
                config_manager.save_config_if_changed("internal_model_settings", settings)
                
                print(f"[LLM Factory] Trying provider: {provider}")
                model = LLMFactory.create_chat_model(
                    internal_role=internal_role,
                    room_name=room_name,
                    temperature=temperature,
                    **kwargs
                )
                
                # 成功したらプロバイダを元に戻す
                settings["provider"] = original_provider
                config_manager.save_config_if_changed("internal_model_settings", settings)
                
                return model
                
            except Exception as e:
                error_msg = f"{provider}: {str(e)}"
                errors.append(error_msg)
                print(f"[LLM Factory] Fallback: Provider '{provider}' failed: {e}")
                # システム通知を追加
                utils.add_system_notice(
                    f"LLM警告: プロバイダ '{provider}' が失敗し、フォールバックを試行しています (原因: {e})",
                    level="warning"
                )
                continue
        
        # すべてのプロバイダで失敗
        raise ValueError(f"All providers failed: {'; '.join(errors)}")