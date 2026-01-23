
import sys
import os

# Add project root to path
sys.path.append(os.getcwd())

from core.mod_loader import ModuleLoader
from core.theme_manager import ThemeManager
from core.event_bus import EventBus
from core.base_module import BaseModule

def verify_foundation():
    print("[-] Initializing Foundation Layer...")
    
    # 1. Initialize Services
    try:
        theme = ThemeManager()
        bus = EventBus()
        print("[+] Services Initialized (ThemeManager, EventBus)")
    except Exception as e:
        print(f"[!] FAILED to initialize services: {e}")
        return False

    context = {
        "theme_manager": theme,
        "event_bus": bus
    }

    # 2. Initialize Loader
    loader = ModuleLoader()
    print("[+] ModuleLoader Initialized")

    # 3. Discover Modules
    modules = loader.discover_modules()
    print(f"[-] Discovered Modules: {modules}")

    # 4. Load Modules
    errors = 0
    for mod_name in modules:
        print(f"[-] Attempting to load: {mod_name}...")
        try:
            # This is the critical test: passing context
            instance = loader.load_module(mod_name, context=context)
            
            if not instance:
                print(f"[!] FAILED to load {mod_name} (Returned None)")
                errors += 1
                continue

            # Verify context injection
            if hasattr(instance, 'context') and instance.context == context:
                print(f"[+] {mod_name} loaded successfully with Context.")
            else:
                print(f"[!] {mod_name} loaded but Context IS MISSING or INCORRECT.")
                errors += 1
                
        except TypeError as te:
            print(f"[!] CRITICAL: Signature Mismatch on {mod_name}.on_load(): {te}")
            errors += 1
        except Exception as e:
            print(f"[!] FAILED to load {mod_name}: {e}")
            errors += 1

    if errors == 0:
        print("\n[SUCCESS] Foundation Verification Passed. All modules accepted the new architecture.")
        return True
    else:
        print(f"\n[FAILURE] Foundation Verification Failed with {errors} errors.")
        return False

if __name__ == "__main__":
    verify_foundation()
