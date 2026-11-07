"""Require a stable, high-confidence recommendation before parsing."""

import time
from collections import OrderedDict

from src.ocr.preprocess import iter_preprocess_recommendation


class OcrRejectedError(RuntimeError):
    pass


# 面板像素 → OCR 证据缓存的最大条目数。同一帧的面板动画期会产生大量
# 只出现一次的哈希，挂机整夜时无界缓存会持续吃内存；条目本身很小，
# 256 足以覆盖"同一画面反复识别"的命中场景，超限逐出最旧的。
_CACHE_MAX_ENTRIES = 256


class StableRecommendationReader:
    def __init__(self, config, backend, sleep=time.sleep,
                 text_normalizer=None, required_headers=(), cache=None,
                 cache_max_entries=_CACHE_MAX_ENTRIES):
        self.config = config
        self.backend = backend
        self.sleep = sleep
        self.text_normalizer = text_normalizer or self._normalize
        self.required_headers = tuple(required_headers)
        # Panel-pixel -> OCR evidence cache. Identical pixels always produce
        # identical OCR output, so a frame whose exact hash was already
        # recognized can skip the (expensive) inference entirely.
        # 注入普通 dict 时保持原语义（不淘汰）；默认自建 OrderedDict 做 LRU。
        self._cache = OrderedDict() if cache is None else cache
        self._cache_max_entries = max(1, int(cache_max_entries))

    def _cache_store(self, frame_hash, evidence):
        self._cache[frame_hash] = evidence
        if isinstance(self._cache, OrderedDict):
            self._cache.move_to_end(frame_hash)
            while len(self._cache) > self._cache_max_entries:
                self._cache.popitem(last=False)

    def _cached(self, frame):
        """Reuse the OCR result for an unchanged panel, or None on miss."""
        frame_hash = getattr(frame, "exact_hash", None)
        if not frame_hash:
            return None
        evidence = self._cache.get(frame_hash)
        if evidence is None:
            return None
        if isinstance(self._cache, OrderedDict):
            self._cache.move_to_end(frame_hash)
        return type(evidence)(
            frame.frame_id, time.time(), evidence.lines,
            evidence.normalized_text, evidence.confidence,
            evidence.backend, evidence.preprocessing)

    def read(self, frame_supplier, roi_supplier):
        previous_text = None
        stable_count = 0
        latest = None
        for attempt in range(self.config.max_attempts):
            frame = frame_supplier()
            roi = roi_supplier(frame)
            evidence = self._cached(frame)
            if evidence is None:
                evidence = self._recognize_roi(roi, frame.frame_id)
                frame_hash = getattr(frame, "exact_hash", None)
                if evidence is not None and frame_hash:
                    self._cache_store(frame_hash, evidence)
            if evidence is None:
                previous_text = None
                stable_count = 0
                if attempt + 1 < self.config.max_attempts:
                    self.sleep(self.config.retry_interval_seconds)
                continue
            text = evidence.normalized_text
            if evidence.confidence >= self.config.min_ocr_confidence and text:
                stable_count = stable_count + 1 if text == previous_text else 1
                previous_text = text
                latest = evidence
                if stable_count >= self.config.stable_frames:
                    return latest
            else:
                previous_text = None
                stable_count = 0
            if attempt + 1 < self.config.max_attempts:
                self.sleep(self.config.retry_interval_seconds)
        raise OcrRejectedError("recommendation_not_stable")

    def read_frame(self, frame, roi_supplier):
        evidence = self._cached(frame)
        if evidence is None:
            evidence = self._recognize_roi(roi_supplier(frame), frame.frame_id)
            frame_hash = getattr(frame, "exact_hash", None)
            if evidence is not None and frame_hash:
                self._cache_store(frame_hash, evidence)
        if evidence is None:
            raise OcrRejectedError("recommendation_not_confident")
        return evidence

    def _recognize_roi(self, roi, frame_id):
        for candidate_name, candidate in iter_preprocess_recommendation(roi):
            evidence = self.backend.recognize(
                candidate, frame_id, candidate_name)
            evidence = self._action_evidence(evidence)
            if (evidence.confidence >= self.config.min_ocr_confidence
                    and evidence.normalized_text):
                return evidence
        return None

    def _action_evidence(self, evidence):
        if self.required_headers:
            header_lines = [
                line for line in evidence.lines
                if line.text.strip() in self.required_headers]
            if (not header_lines or max(
                    line.confidence for line in header_lines)
                    < self.config.min_ocr_confidence):
                return type(evidence)(
                    evidence.frame_id, evidence.created_at, (), "", 0.0,
                    evidence.backend, evidence.preprocessing)
        normalized_text = self.text_normalizer(evidence.normalized_text)
        expected_lines = normalized_text.splitlines()
        relevant_lines = []
        expected_index = 0
        for line in evidence.lines:
            normalized_line = self.text_normalizer(line.text)
            parts = normalized_line.splitlines()
            # A card name is retained only after the contextual '选择卡牌'
            # marker; normalizing that OCR line alone deliberately drops it.
            if (not parts and expected_index > 0
                    and expected_lines[expected_index - 1] == "选择卡牌"):
                parts = self._normalize(line.text).splitlines()
            if not parts:
                continue
            if (expected_lines[expected_index:
                               expected_index + len(parts)] == parts):
                relevant_lines.append(line)
                expected_index += len(parts)
        relevant_lines = tuple(relevant_lines)
        confidence = (min(line.confidence for line in relevant_lines)
                      if relevant_lines else evidence.confidence)
        return type(evidence)(
            evidence.frame_id, evidence.created_at, relevant_lines,
            normalized_text, confidence, evidence.backend,
            evidence.preprocessing)

    @staticmethod
    def _normalize(text):
        return "\n".join(
            line.strip() for line in (text or "").splitlines()
            if line.strip())
