import re

file_path = "/home/baken/nexus_ark/ui_handlers.py"
with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

funcs_to_patch = {
    "handle_save_diary_raw": "text_content",
    "handle_save_identity": "content",
    "handle_save_diary_entry": "new_content",
    "handle_save_notepad_click": "content",
    "handle_save_creative_notes": "content",
    "handle_save_creative_entry": "new_content",
    "handle_save_research_notes": "content",
    "handle_save_research_entry": "new_content",
    "handle_save_core_memory": "content",
    "handle_save_working_memory": "content",
    "handle_save_user_memo": "memo_content"
}

for func_name, var_name in funcs_to_patch.items():
    # Find the function definition
    pattern = rf"(def {func_name}\b.*?:\n(?:\s*(?:\"\"\"|''').*?(?:\"\"\"|''')\n)?)"
    
    def replacer(match):
        prefix = match.group(1)
        # Check if already patched
        if "無効な内容(None)が検知されたため" in content[match.end():match.end()+200]:
            return prefix
            
        patch = f"""    if {var_name} is None or str({var_name}).strip() == "None":
        gr.Warning("無効な内容(None)が検知されたため、データ保護のために保存を中止しました。")
        return {var_name}

"""
        return prefix + patch
        
    content = re.sub(pattern, replacer, content, count=1, flags=re.DOTALL)

with open(file_path, "w", encoding="utf-8") as f:
    f.write(content)
print("Patch applied to ui_handlers.py")
