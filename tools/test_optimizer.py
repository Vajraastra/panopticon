"""Test optimizer with new metadata system."""
import sys
sys.path.insert(0, '.')
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from pathlib import Path
from modules.image_optimizer.logic.optimizer import optimize_image, get_export_path
from core.metadata import MetadataExtractor

# Test with real image
source = Path('tagtest/00011-742884859.png')

# Check source metadata
print('Source metadata:')
bundle = MetadataExtractor.extract(source)
print(f'  Tool: {bundle.tool}')
print(f'  Has Prompts: {bundle.has_prompts()}')
print(f'  Tags: {bundle.tags}')

# Optimize
output = get_export_path(source)
print(f'\nOptimizing to: {output}')

result = optimize_image(str(source), str(output), preserve_metadata=True)

status = "OK" if result["success"] else "FAIL"
print(f'\nResult: {status}')
print(f'  Saved: {result.get("saved_percent", 0):.1f}%')
print(f'  Metadata preserved: {result.get("metadata_preserved", False)}')

# Verify output metadata
print('\nOutput metadata:')
out_bundle = MetadataExtractor.extract(output)
print(f'  Tool: {out_bundle.tool}')
print(f'  Has Prompts: {out_bundle.has_prompts()}')
print(f'  Tags: {out_bundle.tags}')

# Verify integrity
from core.metadata import MetadataVerifier
verify = MetadataVerifier.verify_transfer(source, output)
print(f'\nVerification: {verify.status} ({verify.integrity_score}%)')
