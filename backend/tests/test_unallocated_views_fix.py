"""Tests for unallocated views fix in ABTestService.

This file tests the fix for the issue where manually manipulated 
unallocated views would cause winner determination to fail.
"""

import pytest
from unittest.mock import Mock

from app.models.ab_test import ABTest, ABTestVariant
from app.services.ab_test_service import ABTestService


class TestUnallocatedViewsFix:
    """Test cases for the unallocated views fix."""
    
    def test_worst_case_unallocated_with_excess_clicks(self):
        """Test that _worst_case_unallocated handles excess clicks gracefully."""
        # Create test variants
        leader = Mock()
        leader.views = 1000
        leader.clicks = 50
        
        runner_up = Mock()
        runner_up.views = 800
        runner_up.clicks = 30
        
        # Test case where clicks > views (manual manipulation scenario)
        result = ABTestService._worst_case_unallocated(
            leader, 
            runner_up, 
            unallocated_views=100,
            unallocated_clicks=150  # More clicks than views - this should be adjusted
        )
        
        # Should return valid results instead of failing
        assert isinstance(result, tuple)
        assert len(result) == 3
        
        # The first two elements are the CTR values (should be float)
        leader_ctr, runner_ctr, is_safe = result
        assert isinstance(leader_ctr, (int, float))
        assert isinstance(runner_ctr, (int, float))
        assert isinstance(is_safe, bool)
        
    def test_worst_case_unallocated_normal_case(self):
        """Test that normal case still works correctly."""
        leader = Mock()
        leader.views = 1000
        leader.clicks = 50
        
        runner_up = Mock()
        runner_up.views = 800
        runner_up.clicks = 30
        
        # Normal case where clicks <= views  
        result = ABTestService._worst_case_unallocated(
            leader, 
            runner_up, 
            unallocated_views=100,
            unallocated_clicks=80  # Valid case - clicks <= views
        )
        
        assert isinstance(result, tuple)
        assert len(result) == 3
        
        leader_ctr, runner_ctr, is_safe = result
        assert isinstance(leader_ctr, (int, float))
        assert isinstance(runner_ctr, (int, float))
        assert isinstance(is_safe, bool)

    def test_winner_result_with_manual_unallocated_data(self):
        """Test that winner determination works with manually manipulated data."""
        # Create a mock test with potentially problematic unallocated data
        test = Mock()
        test.variants = [
            Mock(position=0, views=1000, clicks=50, ctr=0.05),
            Mock(position=1, views=800, clicks=30, ctr=0.0375),
        ]
        
        # Set unallocated data that might have been manually manipulated  
        test.unallocated_views = 100
        test.unallocated_clicks = 150  # More clicks than views
        
        # Mock the attributes needed for the test
        test.stats_quality = "stage_attributed"
        test.skip_current_photo = False
        
        # Mock media_state that would normally be there
        test.media_state = {}
        
        # The malformed aggregate must fail closed rather than raising from
        # arithmetic or declaring a winner.
        winner, decision = ABTestService._winner_result(test)
        assert winner is None
        assert decision in {"attribution_uncertainty", "not_statistically_significant", "statistics_not_attributable", "media_not_confirmed"}
    
    def test_worst_case_unallocated_edge_cases(self):
        """Test edge cases in worst_case_unallocated."""
        leader = Mock()
        leader.views = 0
        leader.clicks = 0
        
        runner_up = Mock()
        runner_up.views = 100
        runner_up.clicks = 10
        
        # Test with zero views
        result = ABTestService._worst_case_unallocated(
            leader, 
            runner_up, 
            unallocated_views=0,
            unallocated_clicks=0  
        )
        
        assert isinstance(result, tuple)
        assert len(result) == 3

    def test_worst_case_unallocated_with_negative_values(self):
        """Test that negative values are handled properly."""
        leader = Mock()
        leader.views = 1000
        leader.clicks = 50
        
        runner_up = Mock()
        runner_up.views = 800
        runner_up.clicks = 30
        
        # Test with negative unallocated values - should be handled gracefuly
        result = ABTestService._worst_case_unallocated(
            leader, 
            runner_up, 
            unallocated_views=-50,
            unallocated_clicks=-20  
        )
        
        assert isinstance(result, tuple)
        assert len(result) == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
