"""
Core Metadata Package para Panopticon.
Proporciona herramientas para extracción, escritura y verificación de metadata.
"""

from .bundle import MetadataBundle
from .extractor import MetadataExtractor
from .stamper import StampLib, MetadataStamper, TransferResult
from .verifier import MetadataVerifier, VerificationResult
from .batch_verifier import BatchVerifier, BatchVerificationReport, FileVerificationResult

__all__ = [
    # Data structures
    'MetadataBundle',
    'TransferResult',
    'VerificationResult',
    'FileVerificationResult',
    'BatchVerificationReport',
    
    # Classes
    'MetadataExtractor', 
    'StampLib',
    'MetadataStamper',
    'MetadataVerifier',
    'BatchVerifier',
]
