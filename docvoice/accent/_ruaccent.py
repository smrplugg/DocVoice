"""Расстановка ударений и буквы «ё» по смыслу — перенос RUAccent 1.5.8.3 (MIT, см.
LICENSE-ruaccent.txt).

Отличия от оригинала: токенизация через лёгкую библиотеку tokenizers вместо
transformers; модели не скачиваются здесь (это делает DocVoice); выброшен
движок правил — в оригинале он загружается, но в обработке текста не участвует,
а для работы скачивает из интернета исполняемый код; длинные предложения не
роняют разбор (недостающие предсказания считаются «ставить ударение»).
"""
from __future__ import annotations

import gzip
import json
import re
from os.path import join as join_path

import numpy as np

VOWELS = "аеёиоуыэюяАЕЁИОУЫЭЮЯ"


def _softmax(x, axis=-1):
    e = np.exp(x - np.max(x, axis=axis, keepdims=True))
    return e / e.sum(axis=axis, keepdims=True)


def _session(path: str):
    from onnxruntime import InferenceSession, SessionOptions
    opts = SessionOptions()
    opts.log_severity_level = 3
    return InferenceSession(join_path(path, "model.onnx"), opts, providers=["CPUExecutionProvider"])


def _feed(session, **arrays) -> dict:
    """Только те входы, которые есть у модели (у части моделей нет token_type_ids)."""
    names = {i.name for i in session.get_inputs()}
    return {k: np.asarray(v, dtype=np.int64) for k, v in arrays.items() if k in names}


def _special(path: str, name: str) -> str | None:
    try:
        with open(join_path(path, "special_tokens_map.json"), encoding="utf-8") as f:
            v = json.load(f).get(name)
    except OSError:
        return None
    return v.get("content") if isinstance(v, dict) else v


def fix_capital(source: str, target: str) -> str:
    if len(source) != len(target):
        return target
    return "".join(t.upper() if s.isupper() else t.lower() for s, t in zip(source, target))


# --------------------------------------------------------------------------
# разбиение текста
# --------------------------------------------------------------------------
def split_by_words(string: str):
    string = string.replace(" - ", " ~ ")
    match = list(re.finditer(r"\w*(?:\+\w+)*|[^\w\s]+", string.lower()))
    remaining = [string[a.end():b.start()] for a, b in zip(match, match[1:])]
    words = [string[x.start():x.end()] for x in match]
    mask = [i for i, w in enumerate(words) if w]
    valid = [words[i] for i in mask]
    if not mask:
        return valid, ["", ""]
    res = ["".join(remaining[:mask[0]])] + ["".join(remaining[a + 1:b]) for a, b in zip(mask, mask[1:])]
    res.append("".join(remaining[mask[-1] + 1:]))
    return valid, res


def split_by_sentences(string: str) -> list[str]:
    from razdel import sentenize
    from razdel.substring import Substring
    sentences = list(sentenize(string))
    if not sentences:
        return []
    result = [string[a.stop:b.start] + b.text if a.stop != b.start else b.text
              for a, b in zip([Substring(0, 0, "")] + sentences, sentences)]
    result[-1] = result[-1] + string[sentences[-1].stop:]
    return result


# --------------------------------------------------------------------------
# модели
# --------------------------------------------------------------------------
class AccentModel:
    """Посимвольная модель: ударение в слове, которого нет в словаре."""

    def load(self, path: str):
        self.session = _session(path)
        with open(join_path(path, "config.json"), encoding="utf-8") as f:
            self.id2label = json.load(f)["id2label"]
        with open(join_path(path, "vocab.txt"), encoding="utf-8") as f:
            self.vocab = {line.rstrip("\n"): i for i, line in enumerate(f)}
        self.unk, self.bos, self.eos = self.vocab["[unk]"], self.vocab["[bos]"], self.vocab["[eos]"]

    def put_accent(self, word: str) -> str:
        ids = [self.bos] + [self.vocab.get(c, self.unk) for c in word.lower()] + [self.eos]
        n = len(ids)
        logits = self.session.run(None, _feed(self.session, input_ids=[ids], attention_mask=[[1] * n],
                                              token_type_ids=[[0] * n]))[0]
        scores = _softmax(logits).max(axis=-1)[0]
        labels = logits.argmax(axis=-1)[0]
        text = list(word)
        for i, (label, score) in enumerate(zip(labels, scores)):
            lab = self.id2label[str(int(label))]
            if lab not in ("NO", "STRESS_SECONDARY") and score >= 0.55:
                text[i - 1] = "+" + text[i - 1]
        return "".join(text)


class TokenClassifier:
    """Модели «нужно ли ударение в слове» и «ё или е»: метка на каждое слово."""

    def load(self, path: str):
        from tokenizers import Tokenizer
        self.session = _session(path)
        with open(join_path(path, "config.json"), encoding="utf-8") as f:
            self.id2label = json.load(f)["id2label"]
        self.tok = Tokenizer.from_file(join_path(path, "tokenizer.json"))
        self.tok.no_padding()
        self.tok.no_truncation()
        unk = _special(path, "unk_token")
        self.unk_id = self.tok.token_to_id(unk) if unk else None
        self.subword_prefix = getattr(self.tok.model, "continuing_subword_prefix", None)

    def predict(self, text: str) -> list[str]:
        enc = self.tok.encode(text)
        logits = self.session.run(None, _feed(self.session, input_ids=[enc.ids],
                                              attention_mask=[enc.attention_mask],
                                              token_type_ids=[enc.type_ids]))[0]
        scores = _softmax(logits)[0]
        entities = []
        for idx, token_scores in enumerate(scores):
            if enc.special_tokens_mask[idx]:
                continue
            tid = enc.ids[idx]
            word = self.tok.id_to_token(tid) or ""
            start, end = enc.offsets[idx]
            if self.subword_prefix:
                is_subword = len(word) != len(text[start:end])
            else:
                is_subword = text[start - 1:start] != " " if start > 0 else False
            if tid == self.unk_id:
                is_subword = False
            entities.append((token_scores, is_subword))
        # слово = первая часть + следующие подслова; метка — по средним вероятностям
        out, group = [], []
        for sc, sub in entities:
            if group and not sub:
                out.append(self.id2label[str(int(np.nanmean(np.stack(group), axis=0).argmax()))])
                group = []
            group.append(sc)
        if group:
            out.append(self.id2label[str(int(np.nanmean(np.stack(group), axis=0).argmax()))])
        return out


class OmographModel:
    """Омографы (замок / замок): какой вариант ударения подходит к предложению."""

    SPECIAL = {'балчуга', 'вертела', 'волоки', 'волоку', 'воронью', 'выбродите', 'вывозите', 'выносите',
               'выноситесь', 'выходите', 'железы', 'начала', 'округа', 'перепела', 'развитая', 'развитого',
               'развитое', 'развитой', 'развитом', 'развитому', 'развитою', 'развитую', 'развитые',
               'развитым', 'развитыми', 'развитых', 'сторожа', 'сторожи', 'сторожу', 'удало', 'начался',
               'началась', 'началось', 'бутиках', 'ожила', 'создало', 'коротки', 'проклята', 'роженица',
               'роженицы', 'рожениц', 'роженице', 'роженицам', 'роженицу', 'роженицей', 'роженицею',
               'роженицами', 'роженицах', 'пристава', 'приставов', 'приставам', 'приставами', 'приставах',
               'пережитое', 'пережитого', 'пережитые', 'пережитых', 'пережитому', 'пережитым', 'пережитыми',
               'пережитом', 'нипоняла'}

    def load(self, path: str):
        from tokenizers import Tokenizer
        self.session = _session(path)
        self.tok = Tokenizer.from_file(join_path(path, "tokenizer.json"))
        self.tok.no_padding()
        self.tok.enable_truncation(512)
        pad = _special(path, "pad_token")
        self.pad_id = (self.tok.token_to_id(pad) if pad else None) or 0

    def _probs(self, pairs: list[tuple[str, str]]) -> np.ndarray:
        encs = self.tok.encode_batch(pairs)
        n = max(len(e.ids) for e in encs)
        ids = [e.ids + [self.pad_id] * (n - len(e.ids)) for e in encs]
        mask = [e.attention_mask + [0] * (n - len(e.ids)) for e in encs]
        logits = self.session.run(None, _feed(self.session, input_ids=ids, attention_mask=mask))[0]
        return _softmax(logits)[:, 1]

    def _group_words(self, words):
        result, group = [], [words[0]]
        base = words[0].replace("+", "")

        def flush(group, base):
            if base in self.SPECIAL and len(group) > 3:
                result.extend(group[i:i + 3] for i in range(0, len(group), 3))
            elif len(group) > 3 and len(group) % 2 == 0:
                result.extend(group[i:i + 2] for i in range(0, len(group), 2))
            else:
                result.append(group)

        for w in words[1:]:
            b = w.replace("+", "")
            if b == base:
                group.append(w)
            else:
                flush(group, base)
                group, base = [w], b
        flush(group, base)
        return result

    def classify(self, texts, hypotheses, num_hypotheses):
        texts = [re.sub(r"\s+(?=(?:[,.?!:;…]))", "", t) for t in texts]
        if all(k % 2 == 0 for k in num_hypotheses):
            probs = self._probs(list(zip(texts, hypotheses)))
            return [hypotheses[i] if probs[i] >= probs[i + 1] else hypotheses[i + 1]
                    for i in range(0, len(hypotheses), 2)]
        out, pos = [], 0
        for group in self._group_words(hypotheses):
            t = texts[pos]
            probs = [float(self._probs([(t, h)])[0]) for h in group]
            out.append(group[probs.index(max(probs))])
            pos += len(group)
        return out


# --------------------------------------------------------------------------
# словарь ударений на диске
# --------------------------------------------------------------------------
class AccentDict:
    """Словарь «слово → слово с +» (3,2 млн слов). В памяти Python он занимал бы
    ~800 МБ, поэтому лежит в SQLite: собирается один раз из accents.json.gz."""

    EXTRA = {"о": "+о", "О": "+О"}

    def __init__(self, workdir: str):
        import sqlite3
        db = join_path(workdir, "dictionary", "accents.sqlite")
        if not _sqlite_ready(db):
            build_accent_db(workdir)
        self.conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, check_same_thread=False)

    def get(self, word: str, default=None):
        if word in self.EXTRA:
            return self.EXTRA[word]
        row = self.conn.execute("SELECT v FROM a WHERE k = ?", (word,)).fetchone()
        return row[0] if row else default


def _sqlite_ready(db: str) -> bool:
    import os
    import sqlite3
    if not os.path.isfile(db):
        return False
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:
            return c.execute("SELECT v FROM meta WHERE k = 'done'").fetchone() is not None
    except sqlite3.Error:
        return False


def build_accent_db(workdir: str) -> None:
    """accents.json.gz → accents.sqlite (≈20 секунд, один раз после скачивания)."""
    import os
    import sqlite3
    src = join_path(workdir, "dictionary", "accents.json.gz")
    db = join_path(workdir, "dictionary", "accents.sqlite")
    tmp = db + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    with gzip.open(src) as f:
        data = json.load(f)
    conn = sqlite3.connect(tmp)
    try:
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("CREATE TABLE a (k TEXT PRIMARY KEY, v TEXT) WITHOUT ROWID")
        conn.executemany("INSERT OR REPLACE INTO a VALUES (?, ?)", data.items())
        conn.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO meta VALUES ('done', '1')")
        conn.commit()
    finally:
        conn.close()
    del data
    os.replace(tmp, db)


# --------------------------------------------------------------------------
# всё вместе
# --------------------------------------------------------------------------
class Accentizer:
    NORMALIZE = re.compile(r"[^a-zA-Z0-9\sа-яА-ЯёЁ—.,!?:;\"'(){}\[\]«»„“”-]")
    OMOGRAPH_DIR = "nn/nn_omograph/turbo3.1"

    def load(self, workdir: str):
        def gz(name):
            with gzip.open(join_path(workdir, "dictionary", name)) as f:
                return json.load(f)

        self.omographs = gz("omographs.json.gz")
        self.omographs["коса"] = ["к+оса", "кос+а"]
        self.yo_words = gz("yo_words.json.gz")
        self.yo_homographs = gz("yo_homographs.json.gz")
        self.accents = AccentDict(workdir)
        self.omograph_model = OmographModel()
        self.omograph_model.load(join_path(workdir, self.OMOGRAPH_DIR))
        self.accent_model = AccentModel()
        self.accent_model.load(join_path(workdir, "nn", "nn_accent"))
        self.yo_model = TokenClassifier()
        self.yo_model.load(join_path(workdir, "nn", "nn_yo_homograph_resolver"))
        self.stress_usage = TokenClassifier()
        self.stress_usage.load(join_path(workdir, "nn", "nn_stress_usage_predictor"))

    @staticmethod
    def _delete_spaces_before_punc(text: str) -> str:
        for ch in "!\"#$%&'()*,./:;<=>?@[\\]^_`{|}-":
            if ch == "-":
                text = text.replace(" -", "-").replace("- ", "-")
            text = text.replace(" " + ch, ch)
        return text.replace("~", "-")

    def _process_yo(self, words, sentence):
        lower = sentence.lower()
        preds = self.yo_model.predict(lower) if "е" in lower else None
        for i, word in enumerate(words):
            lw = word.lower()
            words[i] = fix_capital(word, self.yo_words.get(lw, word))
            if preds and i < len(preds) and preds[i] == "YO":
                words[i] = fix_capital(word, self.yo_homographs.get(lw, word))
        return words

    def _process_omographs(self, words):
        found = [(i, self.omographs[w]) for i, w in enumerate(words) if self.omographs.get(w)]
        if not found:
            return words
        texts, hyps, nums = [], [], []
        for pos, variants in found:
            back = words[pos]
            words[pos] = " <w>" + back + "</w> "
            texts += [self._delete_spaces_before_punc(" ".join(words))] * len(variants)
            words[pos] = back
            hyps += variants
            nums.append(len(variants))
        for (pos, _), choice in zip(found, self.omograph_model.classify(texts, hyps, nums)):
            words[pos] = choice
        return words

    def _process_accent(self, words, usages):
        for i, word in enumerate(words):
            if "+" in word or (usages[i] if i < len(usages) else "STRESS") != "STRESS":
                continue
            lw = word.lower()
            stressed = self.accents.get(lw, lw)
            if stressed == lw and not any(c in "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~" for c in lw) \
                    and sum(c in VOWELS for c in lw) > 1:
                words[i] = self.accent_model.put_accent(word)
            else:
                fixed = list(word)
                for j, m in enumerate(re.finditer(r"\+", stressed)):
                    fixed = fixed[:m.start() + j] + ["+"] + list(word)[m.end() - 1:]
                words[i] = "".join(fixed)
        return words

    def _process(self, text: str) -> str:
        text = self.NORMALIZE.sub("", text)
        out = []
        for sentence in split_by_sentences(text):
            words, rest = split_by_words(sentence)
            if not words:
                out.append("".join(rest))
                continue
            usages = self.stress_usage.predict(sentence)
            words = self._process_yo(words, sentence)
            words = self._process_omographs(words)
            words = self._process_accent(words, usages)
            s = "".join([a + b for a, b in zip(rest, words)] + [rest[-1]])
            out.append(self._delete_spaces_before_punc(s))
        return "".join(out)

    def process(self, text: str, skip_regex: str | None = None) -> str:
        """Текст с «+» перед ударными гласными. Куски, подходящие под skip_regex
        (слова с уже поставленным вручную ударением), не трогаются."""
        if not skip_regex:
            return self._process(text)
        parts, last = [], 0
        for m in re.finditer(skip_regex, text):
            if m.start() > last:
                parts.append(self._process(text[last:m.start()]))
            parts.append(m.group(0))
            last = m.end()
        if last < len(text):
            parts.append(self._process(text[last:]))
        return "".join(parts)
