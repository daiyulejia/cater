"""Lossless model JSON formatting, not a remote model quality/cost benchmark."""
from copy import deepcopy
import json
import random
import unittest
from unittest.mock import patch

from cater.team_context import model_context, model_json
from cater.tool_protocol import json_utf8
import test_team_external as external


class ModelJsonTests(unittest.TestCase):
    def test_only_structural_formatting_changes_not_literal_text_or_key_order(self):
        value = {'identity': {'name': '月亮', 'voice': '  自然交流  '},
                 'source': 'first: 1, second: 2\n\t\rC:\\Projects\\a b\\file.py\nprint("a, b: c")',
                 'rows': [None, False, True, 0, -12, 3.5, -0.0, '', '中\U0001f43e文'],
                 'empty': {}, 'other': []}
        before = deepcopy(value)
        encoded = model_json(value)
        self.assertEqual(encoded, json.dumps(value, ensure_ascii=False, separators=(',', ':')))
        self.assertEqual(json.loads(encoded), value)
        self.assertEqual(list(json.loads(encoded)), list(value))
        self.assertEqual(json.loads(encoded)['source'], value['source'])
        self.assertLess(len(encoded.encode('utf-8')), len(json.dumps(value, ensure_ascii=False).encode('utf-8')))
        self.assertEqual(value, before)

    def test_generated_nested_values_decode_with_the_same_types_and_order(self):
        rng = random.Random(836291)
        leaves = [None, False, True, 0, 1, -1, 1.25, -0.0, '', ' : , ', '雪\n光\t\"\\', '\U0001f43e']

        def sample(depth):
            if depth == 0 or rng.randrange(3) == 0:
                return rng.choice(leaves)
            if rng.randrange(2):
                return [sample(depth - 1) for _ in range(rng.randrange(5))]
            return {f'key {index} : ,': sample(depth - 1) for index in range(rng.randrange(5))}

        for index in range(256):
            value = sample(4)
            with self.subTest(index=index):
                ordinary = json.dumps(value, ensure_ascii=False)
                compact = model_json(value)
                options = {'object_pairs_hook': list, 'parse_int': str, 'parse_float': str}
                self.assertEqual(json.loads(compact, **options), json.loads(ordinary, **options))
                self.assertLessEqual(len(compact.encode('utf-8')), len(ordinary.encode('utf-8')))

    def test_existing_nonfinite_and_surrogate_serialization_contract_is_not_broadened(self):
        value = {'unpaired': '\ud800', 'finite': 1.5, 'nan': float('nan'), 'infinite': float('inf')}
        ordinary = json.dumps(value, ensure_ascii=False)
        compact = model_json(value)
        self.assertEqual(json.loads(ordinary, parse_constant=str), json.loads(compact, parse_constant=str))
        for encoded in (ordinary, compact):
            with self.assertRaises(UnicodeEncodeError):
                encoded.encode('utf-8')
        with self.assertRaises(TypeError):
            model_json({'unsupported': object()})

    def test_canonical_bytes_fingerprints_and_surrogate_fallback_keep_the_old_format(self):
        value = {'z': [1, 2], 'a': '月亮'}
        self.assertEqual(json_utf8(value), b'{"z": [1, 2], "a": "\xe6\x9c\x88\xe4\xba\xae"}')
        self.assertEqual(json_utf8(value, sort_keys=True), json.dumps(value, ensure_ascii=False, sort_keys=True).encode())
        self.assertEqual(json_utf8({'unpaired': '\ud800'}), b'{"unpaired": "\\ud800"}')
        with self.assertRaises(ValueError):
            json_utf8({'nan': float('nan')}, allow_nan=False)


class ExternalModelJsonTests(unittest.TestCase):
    def test_external_prompt_keeps_complete_context_and_instructions_with_exact_size_receipt(self):
        fixture = external.TeamExternalTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.driver.scripts[fixture.manager] = [lambda run: '已核对本轮输入，不创建额外工作。']
        captured, raw = [], []
        original_run = external.ScriptedExternalRun.run
        original_input = fixture.rt.team._model_input

        def capture_input(*args, **kwargs):
            result = original_input(*args, **kwargs)
            raw.append(deepcopy(result[0]))
            return result

        def capture_run(run, prompt):
            captured.append(prompt)
            return original_run(run, prompt)

        text = '  先讨论，不开工：a, b: c\n保留缩进\t以及 C:\\a b\\source.py。  '
        with patch.object(fixture.rt.team, '_model_input', side_effect=capture_input), patch.object(
                external.ScriptedExternalRun, 'run', capture_run):
            message = fixture.receive(text)
            fixture.drain()
        self.assertEqual(len(captured), 1)
        instructions, context_text = captured[0].rsplit('\n', 1)
        context = json.loads(context_text)
        self.assertEqual(context, model_context(raw[0]))
        self.assertEqual(context['message']['text'], message.payload['text'])
        self.assertEqual(context_text, json.dumps(context, ensure_ascii=False, separators=(',', ':')))
        self.assertIn('【外部客户端】实际工作只用Cater工具', instructions)
        self.assertIn('临时启动目录不是顾客工程', instructions)
        self.assertIn('完成原委托用return_work交回', instructions)
        receipt, = fixture.events('team.model_called')
        sizes = receipt.payload['request_sizes']
        self.assertEqual(sizes, {
            'prompt_utf8_bytes': len(captured[0].encode('utf-8')),
            'instructions_utf8_bytes': len((instructions + '\n').encode('utf-8')),
            'context_json_bytes': len(context_text.encode('utf-8'))})
        self.assertEqual(sizes['prompt_utf8_bytes'], sizes['instructions_utf8_bytes'] + sizes['context_json_bytes'])
        self.assertTrue(all(type(value) is int and value > 0 for value in sizes.values()))
        self.assertEqual(receipt.payload['token_usage'], 'unavailable')
        self.assertEqual(fixture.events('team.tool_started'), [])
        self.assertEqual(fixture.events('team.objective_completed'), [])


if __name__ == '__main__':
    unittest.main()
