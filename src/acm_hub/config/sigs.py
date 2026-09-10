"""SIG classification rules.

Deterministic and data-driven on purpose: an officer can add a keyword here and
get a predictable, testable behaviour change with no model, no API call and no
inference cost. Terms are matched case-insensitively on word boundaries, so
"ai" does not match "chain" and "ml" does not match "html".
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field


class SigRule(BaseModel):
    """Keyword rules for one Special Interest Group."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sig: str
    display_name: str
    #: Terms that strongly indicate this SIG.
    primary_terms: tuple[str, ...] = ()
    #: Terms that support the SIG but are weaker on their own.
    secondary_terms: tuple[str, ...] = ()


class ClassifierWeights(BaseModel):
    """How much a match contributes, depending on where it was found.

    A term in the title is far more meaningful than the same term buried in a
    description, which is in turn stronger than an upstream category tag.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    title_primary: float = Field(default=1.0, gt=0)
    title_secondary: float = Field(default=0.6, gt=0)
    description_primary: float = Field(default=0.45, gt=0)
    description_secondary: float = Field(default=0.25, gt=0)
    tag_primary: float = Field(default=0.35, gt=0)
    tag_secondary: float = Field(default=0.2, gt=0)
    #: Score at which a SIG is considered a certain match; scores are scaled
    #: against this value and clamped to 1.0.
    saturation: float = Field(default=1.6, gt=0)


DEFAULT_WEIGHTS: Final = ClassifierWeights()

SIG_RULES: Final[tuple[SigRule, ...]] = (
    SigRule(
        sig="SIGAI",
        display_name="Artificial Intelligence",
        primary_terms=(
            "artificial intelligence",
            "machine learning",
            "deep learning",
            "llm",
            "large language model",
            "nlp",
            "natural language processing",
            "computer vision",
            "generative ai",
            "genai",
            "reinforcement learning",
            "ml engineer",
            "ml engineering",
        ),
        secondary_terms=(
            "ai",
            "ml",
            "neural network",
            "transformer",
            "recommendation",
            "speech recognition",
            "model training",
            "pytorch",
            "tensorflow",
            "mlops",
            "diffusion model",
        ),
    ),
    SigRule(
        sig="SIGSOFT",
        display_name="Software Engineering",
        primary_terms=(
            "software engineering",
            "software engineer",
            "software developer",
            "software development",
            "backend",
            "back end",
            "back-end",
            "frontend",
            "front end",
            "front-end",
            "full stack",
            "full-stack",
            "fullstack",
            "developer tools",
            "devops",
            "site reliability",
            "distributed systems",
            "platform engineering",
        ),
        secondary_terms=(
            "cloud",
            "microservices",
            "api",
            "kubernetes",
            "docker",
            "ci/cd",
            "testing",
            "react",
            "typescript",
            "golang",
            "infrastructure",
            "web development",
            "mobile development",
            "compiler",
        ),
    ),
    SigRule(
        sig="SIGHPC",
        display_name="High Performance Computing",
        primary_terms=(
            "high performance computing",
            "high-performance computing",
            "hpc",
            "cuda",
            "parallel computing",
            "performance engineering",
            "supercomputing",
            "kernel optimization",
        ),
        secondary_terms=(
            "gpu",
            "distributed computing",
            "mpi",
            "openmp",
            "simd",
            "throughput",
            "latency optimization",
            "numerical simulation",
            "accelerator",
        ),
    ),
    SigRule(
        sig="SIGKDD",
        display_name="Data Science & Knowledge Discovery",
        primary_terms=(
            "data science",
            "data scientist",
            "data mining",
            "data engineering",
            "data engineer",
            "data analytics",
            "business intelligence",
        ),
        secondary_terms=(
            "analytics",
            "etl",
            "data pipeline",
            "data warehouse",
            "sql",
            "spark",
            "statistics",
            "experimentation",
            "a/b testing",
            "dashboard",
        ),
    ),
    SigRule(
        sig="SIGSAC",
        display_name="Security, Audit & Control",
        primary_terms=(
            "cybersecurity",
            "cyber security",
            "information security",
            "security engineer",
            "security engineering",
            "cryptography",
            "penetration testing",
            "application security",
        ),
        secondary_terms=(
            "security",
            "privacy",
            "vulnerability",
            "threat detection",
            "incident response",
            "malware",
            "encryption",
            "zero trust",
            "identity and access",
        ),
    ),
    SigRule(
        sig="SIGGRAPH",
        display_name="Computer Graphics",
        primary_terms=(
            "computer graphics",
            "rendering",
            "game engine",
            "ray tracing",
            "shader",
            "3d graphics",
            "scientific visualization",
        ),
        secondary_terms=(
            "graphics",
            "visualization",
            "animation",
            "unreal engine",
            "unity",
            "vulkan",
            "opengl",
            "geometry",
            "augmented reality",
            "virtual reality",
        ),
    ),
    SigRule(
        sig="SIGCHI",
        display_name="Human-Computer Interaction",
        primary_terms=(
            "human computer interaction",
            "human-computer interaction",
            "hci",
            "user experience",
            "ux research",
            "ux design",
            "interaction design",
            "usability",
        ),
        secondary_terms=(
            "ux",
            "ui design",
            "user research",
            "design systems",
            "prototyping",
            "user interface",
            "human factors",
        ),
    ),
    SigRule(
        sig="SIGACT",
        display_name="Algorithms & Computation Theory",
        primary_terms=(
            "algorithms",
            "algorithm design",
            "theoretical computer science",
            "computational complexity",
            "combinatorial optimization",
            "formal verification",
        ),
        secondary_terms=(
            "theory",
            "complexity",
            "optimization",
            "graph theory",
            "discrete mathematics",
            "proofs",
            "operations research",
        ),
    ),
    SigRule(
        sig="SIGACCESS",
        display_name="Accessible Computing",
        primary_terms=(
            "accessibility",
            "assistive technology",
            "screen reader",
            "inclusive design",
            "wcag",
        ),
        secondary_terms=(
            "a11y",
            "disability",
            "captioning",
            "adaptive interface",
        ),
    ),
)

#: SIG identifier -> rule, for lookups by name.
SIG_BY_NAME: Final[dict[str, SigRule]] = {rule.sig: rule for rule in SIG_RULES}

#: Ordered SIG identifiers, used to build Discord select menus.
SIG_NAMES: Final[tuple[str, ...]] = tuple(rule.sig for rule in SIG_RULES)
