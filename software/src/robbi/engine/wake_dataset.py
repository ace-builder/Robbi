from __future__ import annotations

import json
import re
import wave
import zipfile
from datetime import datetime
from pathlib import Path

from robbi.configuration.settings import (
    ROOT,
    WAKE_EXPORTS_DIR,
    WAKE_MODELS_DIR,
    WAKE_NEGATIVE_DIR,
    WAKE_NOISE_DIR,
    WAKE_REFERENCES_DIR,
    settings,
)


class WakeDatasetManager:
    CATEGORY_DIRS = {
        "positive": WAKE_REFERENCES_DIR,
        "negative": WAKE_NEGATIVE_DIR,
        "noise": WAKE_NOISE_DIR,
    }

    def list_samples(self, category: str | None = None) -> dict[str, list[dict]]:
        if category is not None:
            return {category: self._list_category(category)}
        return {name: self._list_category(name) for name in self.CATEGORY_DIRS}

    def save_sample(self, category: str, wav_bytes: bytes, sample_name: str | None = None) -> dict:
        target_dir = self._category_dir(category)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / self._next_name(category, sample_name)
        target.write_bytes(wav_bytes)
        return self._sample_info(category, target)

    def delete_sample(self, category: str, name: str) -> dict:
        path = self.sample_file(category, name)
        if path is None:
            raise FileNotFoundError(f"{category} sample not found: {name}")
        path.unlink(missing_ok=True)
        return {"category": category, "name": name}

    def clear_samples(self, category: str) -> int:
        removed = 0
        for item in self._category_dir(category).glob("*.wav"):
            item.unlink(missing_ok=True)
            removed += 1
        return removed

    def sample_file(self, category: str, name: str) -> Path | None:
        target_dir = self._category_dir(category).resolve()
        target = (target_dir / name).resolve()
        try:
            target.relative_to(target_dir)
        except ValueError:
            return None
        return target if target.exists() and target.suffix.lower() == ".wav" else None

    def save_model(self, filename: str, model_bytes: bytes) -> dict:
        safe_name = self._sanitize_filename(filename, suffix=".onnx")
        target = WAKE_MODELS_DIR / safe_name
        target.write_bytes(model_bytes)
        return self.model_status()

    def model_file(self, name: str) -> Path | None:
        target_dir = WAKE_MODELS_DIR.resolve()
        target = (target_dir / name).resolve()
        try:
            target.relative_to(target_dir)
        except ValueError:
            return None
        return target if target.exists() and target.suffix.lower() == ".onnx" else None

    def delete_model(self, name: str) -> dict:
        path = self.model_file(name)
        if path is None:
            raise FileNotFoundError(f"model not found: {name}")
        path.unlink(missing_ok=True)
        return self.model_status()

    def model_status(self) -> dict:
        models = []
        for path in sorted(WAKE_MODELS_DIR.glob("*.onnx")):
            models.append(
                {
                    "name": path.name,
                    "bytes": path.stat().st_size,
                    "updated_at": int(path.stat().st_mtime),
                }
            )
        active_name = settings.voice_nanowake_model.strip()
        if active_name:
            active = active_name
        else:
            active = models[0]["name"] if models else ""
        return {
            "models": models,
            "active_model": active,
            "model_dir": str(WAKE_MODELS_DIR),
        }

    def export_dataset_zip(self) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = WAKE_EXPORTS_DIR / f"robbi-wake-dataset-{timestamp}.zip"
        counts = {name: len(self._list_category(name)) for name in self.CATEGORY_DIRS}
        metadata = {
            "wake_phrase": settings.voice_wake_phrase,
            "exported_at": timestamp,
            "sample_rate": settings.mic_sample_rate,
            "counts": counts,
        }
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for category, directory in self.CATEGORY_DIRS.items():
                for item in sorted(directory.glob("*.wav")):
                    archive.write(item, arcname=f"training_data/{category}/{item.name}")
            archive.writestr("training/metadata.json", json.dumps(metadata, indent=2))
            archive.writestr("training/config.yaml", self._training_config())
            archive.writestr("training/README.txt", self._training_readme())
        return target

    def export_status(self) -> dict:
        exports = []
        for path in sorted(WAKE_EXPORTS_DIR.glob("*.zip"), reverse=True)[:5]:
            exports.append(
                {
                    "name": path.name,
                    "bytes": path.stat().st_size,
                    "updated_at": int(path.stat().st_mtime),
                }
            )
        return {"exports": exports}

    def _list_category(self, category: str) -> list[dict]:
        return [self._sample_info(category, path) for path in sorted(self._category_dir(category).glob("*.wav"))]

    def _sample_info(self, category: str, path: Path) -> dict:
        frames = 0
        sample_rate = settings.mic_sample_rate
        try:
            with wave.open(str(path), "rb") as wav_file:
                frames = wav_file.getnframes()
                sample_rate = wav_file.getframerate()
        except Exception:
            pass
        duration = 0.0
        if sample_rate > 0:
            duration = round(frames / float(sample_rate), 2)
        return {
            "category": category,
            "name": path.name,
            "duration_s": duration,
            "bytes": path.stat().st_size,
            "updated_at": int(path.stat().st_mtime),
        }

    def _category_dir(self, category: str) -> Path:
        normalized = (category or "").strip().lower()
        if normalized not in self.CATEGORY_DIRS:
            raise ValueError(f"unknown wake dataset category: {category}")
        return self.CATEGORY_DIRS[normalized]

    def _next_name(self, category: str, sample_name: str | None) -> str:
        base = self._sanitize_stem(sample_name or category)
        directory = self._category_dir(category)
        existing = sorted(directory.glob(f"{base}-*.wav"))
        next_index = 1
        for path in existing:
            match = re.search(r"-(\d+)\.wav$", path.name)
            if match:
                next_index = max(next_index, int(match.group(1)) + 1)
        return f"{base}-{next_index:02d}.wav"

    def _sanitize_stem(self, value: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", (value or "").strip().lower()).strip("-_")
        return cleaned or "sample"

    def _sanitize_filename(self, filename: str, suffix: str) -> str:
        stem = Path(filename or "").stem
        return f"{self._sanitize_stem(stem)}{suffix}"

    def _training_config(self) -> str:
        lines = [
            'model_name: "robbi_hey_robbi_v1"',
            'model_type: "gru"',
            'output_dir: "./trained_model"',
            'positive_data_path: "./training_data/positive"',
            'negative_data_path: "./training_data/negative"',
            'background_paths:',
            '  - "./training_data/noise"',
            "rir_paths: []",
            f'target_phrase: "{settings.voice_wake_phrase}"',
            "generate_clips: false",
            "transform_clips: true",
            "train_model: true",
            "",
            "checkpointing:",
            "  enabled: true",
            "  interval_steps: 500",
            "  limit: 2",
        ]
        return "\n".join(lines) + "\n"

    def _training_readme(self) -> str:
        return (
            "Robbi wake-word dataset export\n"
            "\n"
            "Training target: your main PC, not the Raspberry Pi Zero 2 W.\n"
            "\n"
            "Suggested workflow:\n"
            "1. Extract this ZIP on your main PC.\n"
            '2. Create a Python environment and install: pip install "nanowakeword[train]"\n'
            "3. Run: nanowakeword-train -c ./training/config.yaml\n"
            "4. After training, copy the exported .onnx model back to Robbi via the /wake page.\n"
            "\n"
            f"Exported from: {ROOT}\n"
        )
