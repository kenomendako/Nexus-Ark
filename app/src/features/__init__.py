from .item_manager import ItemManager

# _recipe_generator は LLMFactory 等に依存するため、遅延インポートとする
# 使用箇所で直接 from src.features._recipe_generator import generate_food_item_profile を行うこと
__all__ = ["ItemManager"]
