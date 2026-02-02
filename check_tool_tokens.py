
import tiktoken
from langchain_core.utils.function_calling import convert_to_openai_function
from agent.graph import all_tools
import json

encoding = tiktoken.get_encoding("cl100k_base")
total_tokens = 0

print(f"Number of tools: {len(all_tools)}")

for tool in all_tools:
    schema = convert_to_openai_function(tool)
    schema_json = json.dumps(schema, ensure_ascii=False)
    tokens = len(encoding.encode(schema_json))
    print(f"Tool: {tool.name}, Tokens: {tokens}")
    total_tokens += tokens

print(f"Total tokens for all tools: {total_tokens}")
print(f"Average tokens per tool: {total_tokens / len(all_tools)}")
