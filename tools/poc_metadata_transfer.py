"""
POC: Metadata Transfer & Verification System
Prueba completa del pipeline de metadata.

Tests:
1. Extracción desde PNG con prompts A1111
2. Transfer metadata a copia
3. Verificación de integridad
4. Strip metadata (Watermarker mode)

Uso:
    python tools/poc_metadata_transfer.py [image_path]
"""
import sys
import os
import shutil
from pathlib import Path

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.paths import CachePaths
from core.metadata import (
    MetadataBundle,
    MetadataExtractor,
    MetadataStamper,
    MetadataVerifier,
    BatchVerifier,
    BatchVerificationReport
)


def print_separator(title: str = ""):
    print("\n" + "=" * 60)
    if title:
        print(f"  {title}")
        print("=" * 60)


def test_extraction(image_path: Path) -> MetadataBundle:
    """Test 1: Extracción de metadata."""
    print_separator("TEST 1: EXTRACTION")
    
    print(f"File: {image_path}")
    print(f"Format: {image_path.suffix.upper()}")
    
    bundle = MetadataExtractor.extract(image_path)
    
    print(f"\nResult: {bundle}")
    print(f"  Valid: {bundle.is_valid()}")
    print(f"  Has Prompts: {bundle.has_prompts()}")
    print(f"  Has Panopticon Data: {bundle.has_panopticon_data()}")
    
    if bundle.positive_prompt:
        prompt_preview = bundle.positive_prompt[:100]
        if len(bundle.positive_prompt) > 100:
            prompt_preview += "..."
        print(f"\n  Positive Prompt: {prompt_preview}")
    
    if bundle.negative_prompt:
        neg_preview = bundle.negative_prompt[:50]
        if len(bundle.negative_prompt) > 50:
            neg_preview += "..."
        print(f"  Negative Prompt: {neg_preview}")
    
    if bundle.model:
        print(f"  Model: {bundle.model}")
    if bundle.seed:
        print(f"  Seed: {bundle.seed}")
    if bundle.tags:
        print(f"  Tags: {bundle.tags}")
    if bundle.rating:
        print(f"  Rating: {'★' * bundle.rating}")
    
    return bundle


def test_transfer(source: Path, dest: Path) -> bool:
    """Test 2: Transferencia de metadata."""
    print_separator("TEST 2: TRANSFER")
    
    print(f"Source: {source}")
    print(f"Dest: {dest}")
    
    # Copy image first (without metadata)
    print("\nCopying image...")
    shutil.copy2(source, dest)
    
    # Transfer metadata
    print("Transferring metadata...")
    result = MetadataStamper.transfer(source, dest)
    
    print(f"\nResult: {'✅ Success' if result.success else '❌ Failed'}")
    if result.error:
        print(f"  Error: {result.error}")
    print(f"  Metadata Preserved: {result.metadata_preserved}")
    
    return result.success


def test_verification(source: Path, dest: Path) -> bool:
    """Test 3: Verificación de integridad."""
    print_separator("TEST 3: VERIFICATION")
    
    print(f"Source: {source}")
    print(f"Dest: {dest}")
    
    result = MetadataVerifier.verify_transfer(source, dest)
    
    print(f"\nResult: {result}")
    print(f"  Status: {result.status}")
    print(f"  Integrity Score: {result.integrity_score}%")
    
    if result.missing:
        print(f"  Missing Fields: {', '.join(result.missing)}")
    
    if result.changed:
        print(f"  Changed Fields:")
        for field, (src, dst) in result.changed.items():
            print(f"    {field}: '{src}' -> '{dst}'")
    
    return result.success


def test_strip(image: Path) -> bool:
    """Test 4: Limpieza de metadata (Watermarker mode)."""
    print_separator("TEST 4: STRIP METADATA")
    
    print(f"File: {image}")
    
    # Check metadata before
    bundle_before = MetadataExtractor.extract(image)
    print(f"\nBefore: {bundle_before}")
    print(f"  Has Data: {bundle_before.is_valid()}")
    
    # Strip
    print("\nStripping metadata...")
    success = MetadataStamper.strip_metadata(image)
    
    print(f"Strip Result: {'✅ Success' if success else '❌ Failed'}")
    
    # Check after
    bundle_after = MetadataExtractor.extract(image)
    print(f"\nAfter: {bundle_after}")
    print(f"  Has Data: {bundle_after.is_valid()}")
    
    return success and not bundle_after.is_valid()


def test_all_formats():
    """Run tests on sample images."""
    print_separator("METADATA SYSTEM POC")
    print("Testing core metadata infrastructure...")
    
    # Setup test directory
    test_dir = CachePaths.get_temp_folder() / "poc_test"
    test_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"\nTest directory: {test_dir}")
    
    # Check for sample images
    sample_dirs = [
        Path(r"D:\githubs\panopticon"),
        Path.home() / "Pictures",
        Path.cwd()
    ]
    
    sample_image = None
    for dir_path in sample_dirs:
        for ext in ['*.png', '*.jpg', '*.webp']:
            files = list(dir_path.glob(ext))
            if files:
                sample_image = files[0]
                break
        if sample_image:
            break
    
    if not sample_image:
        print("\n⚠️ No sample image found. Provide path as argument.")
        print("Usage: python tools/poc_metadata_transfer.py <image_path>")
        return False
    
    print(f"\nUsing sample: {sample_image}")
    
    # Run tests
    results = []
    
    # Test 1: Extraction
    try:
        bundle = test_extraction(sample_image)
        results.append(("Extraction", True))
    except Exception as e:
        print(f"❌ Extraction failed: {e}")
        results.append(("Extraction", False))
        return False
    
    # Test 2: Transfer
    copy_path = test_dir / f"copy{sample_image.suffix}"
    try:
        transfer_ok = test_transfer(sample_image, copy_path)
        results.append(("Transfer", transfer_ok))
    except Exception as e:
        print(f"❌ Transfer failed: {e}")
        results.append(("Transfer", False))
    
    # Test 3: Verification
    if copy_path.exists():
        try:
            verify_ok = test_verification(sample_image, copy_path)
            results.append(("Verification", verify_ok))
        except Exception as e:
            print(f"❌ Verification failed: {e}")
            results.append(("Verification", False))
    
    # Test 4: Strip (on a separate copy)
    strip_path = test_dir / f"strip_test{sample_image.suffix}"
    shutil.copy2(sample_image, strip_path)
    try:
        strip_ok = test_strip(strip_path)
        results.append(("Strip", strip_ok))
    except Exception as e:
        print(f"❌ Strip failed: {e}")
        results.append(("Strip", False))
    
    # Summary
    print_separator("SUMMARY")
    all_passed = True
    for name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {name}: {status}")
        if not passed:
            all_passed = False
    
    print(f"\nOverall: {'✅ All tests passed!' if all_passed else '❌ Some tests failed'}")
    print(f"Test files in: {test_dir}")
    
    return all_passed


def main():
    if len(sys.argv) > 1:
        image_path = Path(sys.argv[1])
        if not image_path.exists():
            print(f"Error: File not found: {image_path}")
            sys.exit(1)
        
        # Run single file tests
        bundle = test_extraction(image_path)
        
        # Optional: run full tests
        test_all_formats()
    else:
        # Run full test suite
        test_all_formats()


if __name__ == "__main__":
    main()
