import unittest

from gorillebot.spam_detector import SpamDetector


class SpamDetectorTests(unittest.TestCase):
    def setUp(self):
        self.detector = SpamDetector()

    def test_apostrophe_signatures_are_detected_after_normalization(self):
        for text in ("J'ai récolté des bénéfices", "C'est vraiment passionnant"):
            with self.subTest(text=text):
                is_spam, _, score = self.detector.is_spam(text)
                self.assertTrue(is_spam)
                self.assertGreaterEqual(score, 80)


if __name__ == "__main__":
    unittest.main()