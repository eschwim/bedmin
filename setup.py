from setuptools import find_packages, setup

setup(
    name="minectl",
    version="1.0.0",
    description="Minecraft Bedrock Dedicated Server manager",
    packages=find_packages(),
    install_requires=[
        "click==8.1.7",
        "requests==2.31.0",
        "tqdm==4.66.1",
        "textual>=0.61.0",
        "schedule>=1.2.0",
    ],
    py_modules=["main", "config", "tui"],
    entry_points={
        "console_scripts": [
            "minectl=main:cli",
            "minectl-tui=tui:main",
        ],
    },
    python_requires=">=3.10",
)
