import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from configs.config_loader import LLMConfig, _dict_to_dataclass
from tools.llm_client import LLMClient


class FlashThinkingControlTests(unittest.TestCase):
    def client(self, model='deepseek-flash', enabled=False):
        client = object.__new__(LLMClient)
        client.model, client.temperature, client.max_tokens = model, .1, 256
        client.enable_flash_thinking_control = enabled
        create = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{}'))], usage=None))
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        return client, create

    def test_alias_is_opt_in_and_only_applies_to_explicit_thinking_false(self):
        for enabled, thinking, expected in ((False, False, False), (True, False, True),
                                            (True, None, False), (True, True, False), ('true', False, False)):
            with self.subTest(enabled=enabled, thinking=thinking):
                client, create = self.client(enabled=enabled)
                client.chat([], thinking=thinking)
                self.assertEqual('extra_body' in create.call_args.kwargs, expected)
                if expected:
                    self.assertEqual(create.call_args.kwargs['extra_body'], {'thinking': {'type': 'disabled'}})

    def test_other_providers_unchanged_and_v4_existing_behavior_preserved(self):
        client, create = self.client(model='other-provider', enabled=True)
        client.chat([], thinking=False)
        self.assertNotIn('extra_body', create.call_args.kwargs)
        client, create = self.client(model='deepseek-v4-pro')
        client.chat([], thinking=False)
        self.assertEqual(create.call_args.kwargs['extra_body'], {'thinking': {'type': 'disabled'}})

    def test_configuration_defaults_off(self):
        self.assertFalse(LLMConfig().enable_flash_thinking_control)
        self.assertTrue(_dict_to_dataclass(LLMConfig, {'enable_flash_thinking_control': True}).enable_flash_thinking_control)


if __name__ == '__main__':
    unittest.main()
