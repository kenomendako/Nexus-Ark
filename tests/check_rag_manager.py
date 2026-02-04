
import sys
sys.path.append("/home/baken/nexus_ark")
try:
    import rag_manager
    print("✅ rag_manager imported successfully.")
except Exception as e:
    print(f"❌ Import failed: {e}")
    sys.exit(1)
