"""
Sentiment analysis layer for K-9 Sentiment Engine.
Provides VADER-based financial sentiment classification,
optional FinBERT integration, trend detection, and aggregate metrics.
"""
import threading
from typing import List, Dict, Any, Optional

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

import config
from scraper import stats

# Global FinBERT model & availability flag
FINBERT_PIPELINE = None
FINBERT_AVAILABLE = False

# Financial & social media lexicon additions for VADER
FINANCIAL_LEXICON = {
    'bullish': 2.0, 'bearish': -2.0, 'bull': 1.5, 'bear': -1.5,
    'moon': 2.0, 'mooning': 2.5, 'rocket': 2.0, '🚀': 2.5, '💎': 2.0,
    '🙌': 1.5, '🐂': 2.0, '🐻': -2.0, 'calls': 1.5, 'puts': -1.5,
    'long': 1.0, 'short': -1.0, 'hodl': 1.5, 'hoddle': 1.5,
    'pump': 1.5, 'dump': -2.0, 'bagholder': -2.0, 'rugpull': -3.0,
    'ath': 2.0, 'btfd': 2.0, 'dip': -0.5, 'outperform': 1.5,
    'underperform': -1.5, 'buy': 1.5, 'sell': -1.5, 'surge': 2.0,
    'crash': -2.5, 'rally': 2.0, 'plunge': -2.0, 'profit': 1.5,
    'loss': -1.5, 'bankrupt': -3.0, 'bankruptcy': -3.0, 'growth': 1.5
}


def _init_finbert():
    """Attempt to initialize FinBERT pipeline via HuggingFace transformers."""
    global FINBERT_PIPELINE, FINBERT_AVAILABLE
    try:
        from transformers import pipeline
        # Attempt lightweight load or device check
        FINBERT_PIPELINE = pipeline("sentiment-analysis", model=config.FINBERT_MODEL)
        FINBERT_AVAILABLE = True
    except Exception:
        FINBERT_PIPELINE = None
        FINBERT_AVAILABLE = False


# Initialize optional FinBERT on module import (safely handled)
_init_finbert()


class SymbolSentiment:
    """Classifies sentiment compound scores into financial labels."""
    @staticmethod
    def classify_score(compound: float) -> str:
        """
        Map compound score to financial label:
        compound > 0.05  -> bullish
        compound < -0.05 -> bearish
        else             -> neutral
        """
        if compound > 0.05:
            return "bullish"
        elif compound < -0.05:
            return "bearish"
        else:
            return "neutral"


class TrendAnalyzer:
    """Tracks and analyzes sentiment trends over time across queries/symbols."""
    def __init__(self):
        self._history: Dict[str, float] = {}
        self._lock = threading.Lock()

    def analyze_trend(self, query_key: str, current_avg: float, posts: List[Dict[str, Any]] = None) -> str:
        """
        Compare current sentiment score with historical average or recent post trajectory.
        Returns: 'improving', 'declining', or 'stable'.
        """
        with self._lock:
            prev_avg = self._history.get(query_key)
            self._history[query_key] = current_avg

        if prev_avg is not None:
            diff = current_avg - prev_avg
            if diff > 0.03:
                return "improving"
            elif diff < -0.03:
                return "declining"
            else:
                return "stable"

        # If no previous score stored, compare first half vs second half of posts if available
        if posts and len(posts) >= 4:
            half = len(posts) // 2
            first_half = posts[:half]
            second_half = posts[half:]

            def get_comp(p):
                return p.get("sentiment", {}).get("compound", 0.0)

            avg_first = sum(get_comp(p) for p in first_half) / max(len(first_half), 1)
            avg_second = sum(get_comp(p) for p in second_half) / max(len(second_half), 1)

            diff = avg_first - avg_second  # first half is newer
            if diff > 0.03:
                return "improving"
            elif diff < -0.03:
                return "declining"

        return "stable"


trend_analyzer = TrendAnalyzer()


class SentimentAnalyzer:
    """Primary sentiment analyzer combining VADER, custom lexicon, and optional FinBERT."""
    def __init__(self):
        self.vader = SentimentIntensityAnalyzer()
        self.vader.lexicon.update(FINANCIAL_LEXICON)

    def analyze_text(self, title: str, text: str) -> Dict[str, Any]:
        """
        Analyze text using VADER (or FinBERT if available) and return normalized sentiment object.
        """
        combined = f"{title}. {text}".strip()
        if not combined or combined == ".":
            return {
                "compound": 0.0,
                "positive": 0.0,
                "negative": 0.0,
                "neutral": 1.0,
                "label": "neutral"
            }

        vader_scores = self.vader.polarity_scores(combined)
        compound = round(float(vader_scores.get("compound", 0.0)), 4)
        pos = round(float(vader_scores.get("pos", 0.0)), 4)
        neg = round(float(vader_scores.get("neg", 0.0)), 4)
        neu = round(float(vader_scores.get("neu", 1.0)), 4)

        # Optional FinBERT blend if available
        if FINBERT_AVAILABLE and FINBERT_PIPELINE is not None:
            try:
                fb_res = FINBERT_PIPELINE(combined[:512])[0]
                fb_label = fb_res.get("label", "").lower()
                fb_score = float(fb_res.get("score", 0.0))
                if fb_label == "positive":
                    compound = round((compound + fb_score) / 2, 4)
                elif fb_label == "negative":
                    compound = round((compound - fb_score) / 2, 4)
            except Exception:
                pass

        label = SymbolSentiment.classify_score(compound)

        return {
            "compound": compound,
            "positive": pos,
            "negative": neg,
            "neutral": neu,
            "label": label
        }

    def analyze_batch(self, posts: List[Dict[str, Any]], query_key: str = "default") -> Dict[str, Any]:
        """
        Analyze a list of post dicts, attach 'sentiment' to each post,
        and calculate aggregate summary metrics.
        """
        analyzed_posts = []
        bullish_count = 0
        bearish_count = 0
        neutral_count = 0
        total_compound = 0.0

        for post in posts:
            title = post.get("title", "")
            text = post.get("text", "")
            sent = self.analyze_text(title, text)

            post_copy = dict(post)
            post_copy["sentiment"] = sent
            analyzed_posts.append(post_copy)

            label = sent["label"]
            if label == "bullish":
                bullish_count += 1
            elif label == "bearish":
                bearish_count += 1
            else:
                neutral_count += 1

            total_compound += sent["compound"]

        total_posts = len(analyzed_posts)
        avg_sentiment = round(total_compound / total_posts, 4) if total_posts > 0 else 0.0
        sentiment_score = avg_sentiment  # normalized -1 to 1

        stats.total_posts_analyzed += total_posts

        trend = trend_analyzer.analyze_trend(query_key, avg_sentiment, analyzed_posts)

        aggregate = {
            "total_posts": total_posts,
            "avg_sentiment": avg_sentiment,
            "bullish_count": bullish_count,
            "bearish_count": bearish_count,
            "neutral_count": neutral_count,
            "sentiment_score": sentiment_score,
            "trend": trend
        }

        return {
            "posts": analyzed_posts,
            "aggregate": aggregate
        }


analyzer = SentimentAnalyzer()
