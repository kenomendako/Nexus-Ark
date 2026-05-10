import os
import re

tools_dir = "/home/baken/nexus_ark/tools"

for filename in ["memory_tools.py", "creative_tools.py", "notepad_tools.py", "research_tools.py"]:
    filepath = os.path.join(tools_dir, filename)
    if not os.path.exists(filepath): continue
    
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # Look for: if content and str(content).strip():
    pattern = r"if content and str\(content\)\.strip\(\):"
    replacement = r"if content and str(content).strip() and str(content).strip() != 'None':"
    
    new_content = re.sub(pattern, replacement, content)
    
    if new_content != content:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(new_content)
        print(f"Patched {filename}")

