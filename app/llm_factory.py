# llm_factory.py

import os
from typing import Any
from langchain_google_genai import ChatGoogleGenerativeAI, HarmBlockThreshold, HarmCategory
from langchain_openai import ChatOpenAI
# from langchain_anthropic import ChatAnthropic # 遅延読み込みに変更
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
            res = config_manager.get_effective_internal_model(internal_role)
            active_provider, effective_model_name, internal_profile_name = res
            
            print(f"--- [LLM Factory] 内部モデル設定（混合編成）適用: {internal_role} ---")
            print(f"  - Provider Category: {active_provider}")
            if active_provider in ["openai", "openai_official", "anthropic"]:
                print(f"  - Profile: {internal_profile_name}")
            print(f"  - Model: {effective_model_name}")
            
            # 2. モデル名のサニタイズ
            sanitized_model_name = utils.sanitize_model_name(effective_model_name or "")

            # 3. プロバイダごとの分岐処理
            if active_provider == "google" or active_provider == "Google (Gemini)":
                # [2026-02-21 FIX] プロバイダ直接指定時でも、キーが枯渇していれば自動フォールバックを試みる
                if api_key:
                    key_name_temp = config_manager.get_key_name_by_value(api_key)
                    if key_name_temp and config_manager.is_key_exhausted(key_name_temp, model_name=sanitized_model_name):
                        print(f"--- [LLM Factory] (Role: {internal_role}) Provided API key '{key_name_temp}' is exhausted. Switching to rotation. ---")
                        api_key = None

                if not api_key:
                    # [2026-02-01 FIX] 内部処理(internal_role)ではルーム個別設定ではなく共通設定のAPIキーを優先する
                    api_key = config_manager.get_active_gemini_api_key(None, model_name=sanitized_model_name)
                if not api_key:
                    raise ValueError("Google provider requires an API key. No valid key found.")
                return gemini_api.get_configured_llm(
                    model_name=sanitized_model_name,
                    api_key=api_key,
                    generation_config=generation_config or {}
                )
            elif active_provider == "local":
                local_model_path = config_manager.LOCAL_MODEL_PATH
                if local_model_path:
                    local_model_path = local_model_path.replace("\\", "/") # Windowsパスの自動補正
                if not local_model_path or not os.path.exists(local_model_path):
                    raise ValueError(f"Local LLM requires a valid GGUF model path. Current: '{local_model_path}'")
                try:
                    from langchain_community.chat_models import ChatLlamaCpp
                    return ChatLlamaCpp(
                        model_path=local_model_path,
                        temperature=temperature,
                        n_ctx=36000,
                        n_gpu_layers=-1,
                        verbose=False
                    )
                except ImportError as e:
                    raise ValueError(f"llama-cpp-python is not installed. (Internal) Details: {e}")
            elif active_provider == "anthropic":
                anthropic_api_key = config_manager.ANTHROPIC_API_KEY
                if not anthropic_api_key:
                    raise ValueError("Anthropic provider requires an API key.")
                from langchain_anthropic import ChatAnthropic
                return ChatAnthropic(
                    model_name=sanitized_model_name,
                    anthropic_api_key=anthropic_api_key,
                    temperature=temperature,
                    top_p=top_p
                )
            elif active_provider == "openai" or active_provider == "openai_official":
                # 指定されたプロファイルの設定を取得
                openai_setting = config_manager.get_openai_setting_by_name(internal_profile_name)
                if not openai_setting:
                    # フォールバック: アクティブなプロバイダ設定
                    openai_setting = config_manager.get_active_openai_setting()
                
                if not openai_setting:
                    raise ValueError(f"OpenAI profile '{internal_profile_name}' not found.")
                
                # openai_official の場合は公式URLを強制
                base_url = openai_setting.get("base_url")
                if active_provider == "openai_official":
                    base_url = "https://api.openai.com/v1"
                
                return ChatOpenAI(
                    base_url=base_url,
                    api_key=openai_setting.get("api_key") or "dummy",
                    model=sanitized_model_name,
                    temperature=openai_setting.get("temperature", temperature),
                    top_p=openai_setting.get("top_p", top_p),
                    max_tokens=openai_setting.get("max_tokens"),
                    streaming=True
                )
            else:
                # 互換性維持: active_provider がプロファイルの可能性（旧形式）
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

                # パラメータ最適化とプロファイル設定の適用
                target_temp = openai_setting.get("temperature", temperature)
                target_top_p = openai_setting.get("top_p", top_p)
                max_tokens = openai_setting.get("max_tokens", None)
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
                    max_tokens=max_tokens,
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
        if active_provider == "google" or active_provider == "Google (Gemini)":
            key_name_for_log = "Unknown"
            
            # [2026-02-21 FIX] 既にAPIキーが渡されていても、枯渇している場合は自動補完（ローテーション）に委ねる
            if api_key:
                key_name_temp = config_manager.get_key_name_by_value(api_key)
                if key_name_temp and config_manager.is_key_exhausted(key_name_temp, model_name=internal_model_name):
                    print(f"--- [LLM Factory] Provided API key '{key_name_temp}' is exhausted for '{internal_model_name}'. Falling back to rotation. ---")
                    api_key = None

            # api_key が未指定なら自動補完
            if not api_key:
                api_key = config_manager.get_active_gemini_api_key(room_name, model_name=internal_model_name)
                # 設定から取得した場合、そのコンテキストでの名前も取得
                key_name = config_manager.get_active_gemini_api_key_name(room_name, model_name=internal_model_name)
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

        # --- Local GGUF (llama-cpp-python) ---
        elif active_provider == "local":
            local_model_path = config_manager.LOCAL_MODEL_PATH
            if local_model_path:
                local_model_path = local_model_path.replace("\\", "/") # Windowsパスの自動補正
            
            if not local_model_path or not os.path.exists(local_model_path):
                raise ValueError(f"Local LLM requires a valid GGUF model path. Current: '{local_model_path}'")
            
            print(f"--- [LLM Factory] Initializing Local GGUF Model ---")
            print(f"  - Model Path: {local_model_path}")
            
            try:
                from langchain_community.chat_models import ChatLlamaCpp
                return ChatLlamaCpp(
                    model_path=local_model_path,
                    temperature=temperature,
                    n_ctx=36000, # Nexus Arkのプロンプト（30,000トークン超）を収めるため拡張
                    n_gpu_layers=-1, # GPUをフル活用
                    verbose=False
                )
            except ImportError as e:
                import traceback
                print("--- [ERROR] 'local' provider model loading failed ---")
                traceback.print_exc()
                raise ValueError(f"llama-cpp-python is not installed or import failed. Details: {e}")

        # --- OpenAI Compatible (OpenRouter, Groq, Ollama, etc.) ---
        elif active_provider == "openai":
            # ルーム個別のOpenAI設定を優先
            # generation_configにopenai_settingsが含まれていればそれを使用
            openai_setting = None  # デフォルトで初期化（後段での参照エラーを防止）
            room_openai_settings = None
            if generation_config and isinstance(generation_config, dict):
                room_openai_settings = generation_config.get("openai_settings")
            
            if room_openai_settings and room_openai_settings.get("base_url"):
                # ルーム個別設定を使用
                base_url = room_openai_settings.get("base_url")
                openai_api_key = room_openai_settings.get("api_key", "dummy")
                provider_name = room_openai_settings.get("name", "Room-specific")
                
                # [Dynamic Injection] ルーム個別設定でも特定のプロバイダの場合はグローバルキーを優先（401対策）
                if provider_name == "Pollinations.ai" or "pollinations" in base_url.lower():
                    global_key = config_manager.CONFIG_GLOBAL.get("pollinations_api_key")
                    if global_key and openai_api_key in ["pollinations", "dummy", ""]:
                        openai_api_key = global_key
                elif provider_name == "Zhipu AI":
                    global_key = config_manager.CONFIG_GLOBAL.get("zhipu_api_key")
                    if global_key and openai_api_key in ["dummy", ""]:
                        openai_api_key = global_key
                elif provider_name == "Groq":
                    global_key = config_manager.CONFIG_GLOBAL.get("groq_api_key")
                    if global_key and openai_api_key in ["dummy", ""]:
                        openai_api_key = global_key
                elif provider_name == "Moonshot AI":
                    global_key = config_manager.CONFIG_GLOBAL.get("moonshot_api_key")
                    if global_key and openai_api_key in ["dummy", ""]:
                        openai_api_key = global_key

                # ルーム個別設定をプロファイルとしても扱う（temperature等の反映用）
                openai_setting = room_openai_settings
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
            
            # (1) UI等からの指定パラメータ (デフォルト値)
            target_temp = temperature
            target_top_p = top_p
            max_tokens = None
            
            # (2) プロファイル設定での上書き
            if openai_setting:
                if "temperature" in openai_setting: target_temp = openai_setting["temperature"]
                if "top_p" in openai_setting: target_top_p = openai_setting["top_p"]
                if "max_tokens" in openai_setting: max_tokens = openai_setting["max_tokens"]
            
            if generation_config and isinstance(generation_config, dict):
                for k, v in generation_config.items():
                    if k in openai_whitelist:
                        model_kwargs[k] = v
                
                # generation_config (Agent側の指示) は最強
                if "max_tokens" in generation_config:
                    max_tokens = generation_config["max_tokens"]

            # Zhipu AI 向けの最適化
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

        # --- Anthropic ---
        elif active_provider == "anthropic":
            anthropic_api_key = config_manager.ANTHROPIC_API_KEY
            if not anthropic_api_key:
                raise ValueError("Anthropic provider requires an API key. No valid key found.")
            
            print(f"--- [LLM Factory] Creating Anthropic client ---")
            print(f"  - Model: {internal_model_name}")
            
            # [2026-04-30 FIX] モデル名の不整合チェック (例: google/geminiモデルが渡された場合)
            is_mismatch = any(kw in internal_model_name.lower() for kw in ["gemini", "gpt", "glm", "llama", "deepseek"])
            if is_mismatch:
                fallback_model = config_manager.CONFIG_GLOBAL.get("anthropic_default_model", "claude-3-7-sonnet-20250219")
                print(f"--- [警告] モデル名の不整合を検知: '{internal_model_name}' は Anthropic プロバイダに不適切です。 ---")
                print(f"--- [警告] デフォルトモデル '{fallback_model}' にフォールバックします。 ---")
                internal_model_name = fallback_model
            
            from langchain_anthropic import ChatAnthropic
            
            # Anthropic用パラメータ
            model_kwargs = {}
            anthropic_whitelist = ["top_k", "stop_sequences"]
            max_tokens = 4096 # Anthropic default
            
            if generation_config and isinstance(generation_config, dict):
                for k, v in generation_config.items():
                    if k in anthropic_whitelist:
                        model_kwargs[k] = v
                if "max_tokens" in generation_config:
                    max_tokens = generation_config["max_tokens"]
                    
            from langchain_anthropic import ChatAnthropic
            
            # Anthropic は temperature と top_p の同時指定を許可しない場合があるため、
            # 優先度の高い temperature のみを指定するように修正。
            # (UIで両方が指定されていても、Anthropicの制約に従い片方のみを送信する)
            return ChatAnthropic(
                model_name=internal_model_name,
                anthropic_api_key=anthropic_api_key,
                temperature=temperature,
                max_tokens=max_tokens,
                # top_p=top_p, # 同時指定不可のためコメントアウト
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
        
        provider_key_map = {
            "processing": "processing_provider",
            "summarization": "summarization_provider",
            "supervisor": "supervisor_provider",
            "translation": "translation_provider",
        }
        
        if internal_role and internal_role in provider_key_map:
            provider_key = provider_key_map[internal_role]
            primary_provider, _, _ = config_manager.get_effective_internal_model(internal_role)
        else:
            provider_key = "provider"
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
                original_provider = settings.get(provider_key)
                settings[provider_key] = provider
                config_manager.save_config_if_changed("internal_model_settings", settings)
                
                print(f"[LLM Factory] Trying provider: {provider} (Role: {internal_role})")
                model = LLMFactory.create_chat_model(
                    internal_role=internal_role,
                    room_name=room_name,
                    temperature=temperature,
                    **kwargs
                )
                
                # 成功したらプロバイダを元に戻す
                if original_provider is not None:
                    settings[provider_key] = original_provider
                else:
                    settings.pop(provider_key, None)
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

    @staticmethod
    def invoke_internal_llm(
        internal_role: str,
        prompt: Any,
        room_name: str = None,
        generation_config: dict = None,
        api_key: str = None,
        max_retries: int = None,
        temperature: float = None,
        top_p: float = None
    ) -> Any:
        """
        [Phase 4] 内部モデル設定に基づき、APIキーの枯渇チェックとローテーションを行いながら
        LLMを呼び出す。
        
        Returns:
            (response, used_api_key): レスポンスオブジェクトと実際に使用されたAPIキー
        """
        import time
        
        # 1. 実行設定の取得
        import config_manager
        
        provider_cat, _, _ = config_manager.get_effective_internal_model(internal_role)
        is_google = provider_cat in ["google", "Google (Gemini)", "Google (Gemini Native)"]
        
        # 2. 初期APIキーの特定
        if is_google and not api_key:
            # 内部処理(internal_role)ではルーム個別設定ではなく共通設定のAPIキーを優先する
            api_key = config_manager.get_active_gemini_api_key(None)
            
        current_api_key = api_key
        current_key_name = config_manager.get_key_name_by_value(current_api_key)
        
        tried_keys = set()
        if current_key_name != "Unknown":
            tried_keys.add(current_key_name)
            
        # モデル名を特定（枯渇管理用）
        _, effective_model_name, _ = config_manager.get_effective_internal_model(internal_role)
        sanitized_model_name = utils.sanitize_model_name(effective_model_name or "")
            
        # 全キーを1周試してもダメなら諦めるための最大回数
        all_keys_count = len(config_manager.GEMINI_API_KEYS)
        if max_retries is None:
            max_retries = max(5, all_keys_count)
            
        last_error = None
        for attempt in range(max_retries):
            # A. 枯渇チェック (Googleのみ)
            if is_google and config_manager.is_key_exhausted(current_key_name, model_name=sanitized_model_name):
                print(f"  [LLM Factory Rotation] Key '{current_key_name}' is exhausted for model '{sanitized_model_name}'. Swapping...")
                next_key = config_manager.get_next_available_gemini_key(
                    current_exhausted_key=current_key_name,
                    excluded_keys=tried_keys,
                    model_name=sanitized_model_name
                )
                if next_key:
                    current_key_name = next_key
                    current_api_key = config_manager.GEMINI_API_KEYS[next_key]
                    tried_keys.add(next_key)
                else:
                    print(f"  [LLM Factory Rotation] No more available keys. Tried {len(tried_keys)} keys.")
                    raise Exception("利用可能なAPIキーがありません（全キー試行済み、または枯渇）。")

            # B. モデル生成
            try:
                llm = LLMFactory.create_chat_model(
                    api_key=current_api_key,
                    generation_config=generation_config,
                    internal_role=internal_role,
                    room_name=room_name,
                    temperature=temperature,
                    top_p=top_p
                )
                
                # C. 実行
                # 戻り値として (response, used_api_key) を返す
                return llm.invoke(prompt), current_api_key
            except Exception as e:
                last_error = e
                err_str = str(e).upper()
                
                # 429: 枯渇
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "TOO_MANY_REQUESTS" in err_str:
                    if is_google:
                        print(f"  [LLM Factory Rotation] 429 Error with key '{current_key_name}' for model '{sanitized_model_name}'.")
                        config_manager.mark_key_as_exhausted(current_key_name, model_name=sanitized_model_name)
                    else:
                        print(f"  [LLM Factory] Rate limit error (429) for non-Google provider. Retrying...")
                    time.sleep(2 * (attempt + 1))
                    continue
                
                # 503/502/504: 一時的なサーバーエラー または ネットワーク接続エラー
                elif any(code in err_str for code in ["502", "503", "504", "500"]) or \
                     any(msg in err_str for msg in ["CONNECTION ERROR", "UNAVAILABLE", "GETADDRINFO", "TIMEOUT", "CONNECTERROR", "PEER RESET"]):
                    
                    if is_google:
                        print(f"  [LLM Factory Rotation] Server/Connection error with key '{current_key_name}'. Details: {str(e)[:150]}")
                        print(f"  [LLM Factory Rotation] Swapping key temporarily for retry...")
                        next_key = config_manager.get_next_available_gemini_key(
                            current_exhausted_key=current_key_name,
                            excluded_keys=tried_keys,
                            model_name=sanitized_model_name
                        )
                        if next_key:
                            current_key_name = next_key
                            current_api_key = config_manager.GEMINI_API_KEYS[next_key]
                            tried_keys.add(next_key)
                    else:
                        print(f"  [LLM Factory] Server/Connection error. Details: {str(e)[:100]}")
                        print(f"  [LLM Factory] Retrying... ({attempt+1}/{max_retries})")
                    
                    time.sleep(2 * (attempt + 1))
                    continue
                else:
                    # その他の致命的なエラー
                    raise e
                    
        if last_error:
            raise last_error
        raise Exception(f"Max retries exceeded in LLMFactory.invoke_internal_llm (Role: {internal_role})")