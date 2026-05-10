import os
import re

tools_dir = "/home/baken/nexus_ark/tools"

for filename in ["memory_tools.py", "notepad_tools.py"]:
    filepath = os.path.join(tools_dir, filename)
    if not os.path.exists(filepath): continue
    
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # Look for: text = inst.get("content", "")
    # if text and str(text).strip():
    pattern = r"if (text|raw_content) and str\(\1\)\.strip\(\):"
    replacement = r"if \1 and str(\1).strip() and str(\1).strip() != 'None':"
    
    new_content = re.sub(pattern, replacement, content)
    
    if new_content != content:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(new_content)
        print(f"Patched {filename}")

