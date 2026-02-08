import os
import sys
import platform
import subprocess
import shutil

def is_windows():
    return platform.system().lower() == "windows"

def check_nvidia_gpu():
    """Simple check for NVIDIA GPU using nvidia-smi"""
    if shutil.which("nvidia-smi"):
        try:
            subprocess.check_call(["nvidia-smi"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except subprocess.CalledProcessError:
            return False
    return False

def install_llama_cpp(use_cuda=False):
    """Install llama-cpp-python with appropriate options"""
    print(f"Installing llama-cpp-python (CUDA={use_cuda})...")
    
    cmd = ["uv", "pip", "install"]
    
    if is_windows():
        # Windows specific handling
        if use_cuda:
            # Assumes CUDA 12.x which is common now. 
            # Using abetlen's pre-built wheels for convenience
            # https://github.com/abetlen/llama-cpp-python/releases
            # Note: We are finding a generic CUDA 12 wheel.
            # Using the official extra-index-url for pre-built wheels
            cmd.extend([
                "llama-cpp-python",
                "--extra-index-url", "https://abetlen.github.io/llama-cpp-python/whl/cu124"
            ])
        else:
            # CPU only for Windows
            cmd.extend([
                "llama-cpp-python",
                "--extra-index-url", "https://abetlen.github.io/llama-cpp-python/whl/cpu"
            ])
    else:
        # Linux/Mac - usually build from source works better or let uv handle it
        # For Linux with CUDA, we need to set CMAKE_ARGS
        if use_cuda:
             os.environ["CMAKE_ARGS"] = "-DGGML_CUDA=on"
             print("Set CMAKE_ARGS=-DGGML_CUDA=on for compilation")
        
        cmd.append("llama-cpp-python")

    print(f"Running: {' '.join(cmd)}")
    try:
        subprocess.check_call(cmd)
        print("\nSUCCESS: llama-cpp-python installed successfully!")
    except subprocess.CalledProcessError as e:
        print(f"\nERROR: Installation failed with exit code {e.returncode}")
        print("Please check your internet connection and python environment.")
        sys.exit(1)

def main():
    print("=== Nexus Ark Local LLM Setup Tool ===")
    print("This tool will install the necessary libraries to run Local LLMs (GGUF).")
    print("----------------------------------------")

    has_gpu = check_nvidia_gpu()
    use_cuda = False

    if has_gpu:
        print("✅ NVIDIA GPU detected!")
        while True:
            choice = input("Do you want to enable GPU acceleration (CUDA)? (y/n): ").lower().strip()
            if choice in ['y', 'yes']:
                use_cuda = True
                break
            elif choice in ['n', 'no']:
                use_cuda = False
                break
    else:
        print("Checking for GPU... No NVIDIA GPU detected (or nvidia-smi not found).")
        print("Installing CPU-only version.")
        use_cuda = False

    install_llama_cpp(use_cuda)
    
    print("\nSetup complete. You can now use Local LLM in Nexus Ark.")
    if is_windows():
        input("Press Enter to close...")

if __name__ == "__main__":
    main()
