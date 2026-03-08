# tools/entity_tools.py

from langchain.tools import tool
from entity_memory_manager import EntityMemoryManager
import traceback

@tool
def read_entity_memory(entity_name: str, room_name: str) -> str:
    """
    Reads the detailed memory about a specific entity (person, topic, or concept).
    Use this when you need deep context about someone or something mentioned in the conversation.
    """
    try:
        manager = EntityMemoryManager(room_name)
        return manager.read_entry(entity_name)
    except Exception as e:
        return f"Error reading entity memory: {str(e)}"

@tool
def write_entity_memory(entity_name: str, content: str, room_name: str, append: bool = True, consolidate: bool = False, api_key: str = None) -> str:
    """
    Writes or updates information about a specific entity. 
    Use this to 'save' important facts, observations, or summaries about a person or topic for future reference.
    - Setting append=True (default) adds new information at the end.
    - Setting consolidate=True will merge and summarize existing memory with new info (requires api_key).
    """
    try:
        manager = EntityMemoryManager(room_name)
        return manager.create_or_update_entry(entity_name, content, append=append, consolidate=consolidate, api_key=api_key)
    except Exception as e:
        return f"Error writing entity memory: {str(e)}"

@tool
def list_entity_memories(room_name: str) -> str:
    """
    Lists all entities that have a recorded memory path.
    Use this to see what 'topics' or 'people' you have structured knowledge about.
    """
    try:
        manager = EntityMemoryManager(room_name)
        entities = manager.list_entries()
        if not entities:
            return "No entity memories recorded yet."
        return "Recorded entities: " + ", ".join(entities)
    except Exception as e:
        return f"Error listing entity memories: {str(e)}"

@tool
def search_entity_memory(query: str, room_name: str) -> str:
    """
    Searches for entities related to the given query.
    Returns a list of entity names that might be relevant.
    """
    try:
        manager = EntityMemoryManager(room_name)
        results = manager.search_entries(query)
        if not results:
            return f"No entity memories found matching '{query}'."
        return "Potential entity matches: " + ", ".join(results)
    except Exception as e:
        return f"Error searching entity memory: {str(e)}"
