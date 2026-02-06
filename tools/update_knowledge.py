import os
import shutil
import sys

def update_olivie_knowledge():
    print("🧠 Starting Knowledge Update for Olivie...")
    
    # Paths
    # Assumes running from project root via `uv run tools/update_knowledge.py` or directly
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)
    
    spec_src = os.path.join(project_root, "docs", "NEXUS_ARK_SPECIFICATION.md")
    target_persona_dir = os.path.join(project_root, "characters", "Olivie")
    target_knowledge_file = os.path.join(target_persona_dir, "knowledge", "NEXUS_ARK_SPECIFICATION.md")

    if not os.path.exists(spec_src):
        print(f"❌ Specification file not found at: {spec_src}")
        return False

    target_persona_dirs = []
    # Support both English and Japanese folder names
    for name in ["Olivie", "オリヴェ"]:
        d = os.path.join(project_root, "characters", name)
        if os.path.exists(d):
            target_persona_dirs.append(d)

    if not target_persona_dirs:
        print(f"ℹ️ Olivie character directory not found (checked 'Olivie' and 'オリヴェ'). Skipping update.")
        # This is expected for new users who haven't initialized yet, or users who deleted Olivie
        return True 

    success_count = 0
    for target_dir in target_persona_dirs:
        target_knowledge_file = os.path.join(target_dir, "knowledge", "NEXUS_ARK_SPECIFICATION.md")
        # Ensure knowledge dir exists
        dest_dir = os.path.dirname(target_knowledge_file)
        os.makedirs(dest_dir, exist_ok=True)
        
        try:
            shutil.copy2(spec_src, target_knowledge_file)
            print(f"✅ Successfully updated knowledge base in: {os.path.basename(target_dir)}")
            success_count += 1
        except Exception as e:
            print(f"❌ Failed to update knowledge for {os.path.basename(target_dir)}: {e}")

    return success_count > 0

if __name__ == "__main__":
    success = update_olivie_knowledge()
    if not success:
        sys.exit(1)
