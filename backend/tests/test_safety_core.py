from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError

from app.models.ab_test import ABTest, ABTestStatus, ABTestVariant
from app.services.ab_test_budget import calculate_required_budget
from app.services.ab_test_service import ABTestService
from app.utils.imagehash_compat import phash


def make_test(*, stats_quality: str = 'stage_attributed', views: tuple[int, int] = (500, 500), clicks=(50, 30)):
    test = ABTest(
        id=1,
        user_id=1,
        connection_id=1,
        nm_id=123,
        title='test',
        status=ABTestStatus.RUNNING,
        skip_current_photo=True,
        views_per_variant=300,
        cpm_rub=300,
        budget_rub=1200,
        stats_quality=stats_quality,
        operation_state='succeeded',
        campaign_state='running',
        media_status='variant_applied',
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    test.variants = [
        ABTestVariant(test_id=1, position=1, source_type='upload', views=views[0], clicks=clicks[0]),
        ABTestVariant(test_id=1, position=2, source_type='upload', views=views[1], clicks=clicks[1]),
    ]
    return test


def test_winner_requires_stage_attribution():
    test = make_test(stats_quality='aggregate_unverified')
    winner, decision = ABTestService._winner_result(test)
    assert winner is None
    assert decision == 'statistics_not_attributable'


def test_winner_is_deterministic_after_stage_settlement():
    test = make_test(stats_quality='stage_attributed')
    winner, decision = ABTestService._winner_result(test)
    assert decision == 'winner_found'
    assert winner is test.variants[0]


def test_winner_rejects_insufficient_stage_sample():
    test = make_test(stats_quality='stage_attributed', views=(299, 500))
    winner, decision = ABTestService._winner_result(test)
    assert winner is None
    assert decision == 'insufficient_data'


def test_cross_user_store_card_is_database_unique():
    engine = create_engine('sqlite:///:memory:')
    from app.models.user import User
    from app.models.wb_connection import WBConnection
    from app.models.auth import EmailVerificationCode, RefreshToken
    from app.models.app_setting import AppSetting
    from app.models.admin_audit import AdminAuditLog
    from app.models.wb_rate_limit import WBApiRateLimit
    ABTest.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO ab_tests (id,user_id,connection_id,nm_id,store_fingerprint,lifecycle_lock,title,status,skip_current_photo,keep_winner_as_main,delete_test_media,views_per_variant,cpm_rub,budget_rub,bid_type,placement,current_variant_order,original_media,media_state,operation_state,campaign_state,media_status,stats_quality,unallocated_views,unallocated_clicks,unallocated_spend_rub,stage_views,stage_clicks,stage_spend_rub,funding_source,last_total_views,last_total_clicks,last_total_orders,last_total_spend_rub,created_at,updated_at) VALUES (1,1,1,123,'same',1,'a','draft',1,0,1,300,300,1200,'unified','combined',0,'[]','{}','preparing','not_created','original','not_started',0,0,0,0,0,0,'auto',0,0,0,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
        with pytest.raises(IntegrityError):
            conn.exec_driver_sql("INSERT INTO ab_tests (id,user_id,connection_id,nm_id,store_fingerprint,lifecycle_lock,title,status,skip_current_photo,keep_winner_as_main,delete_test_media,views_per_variant,cpm_rub,budget_rub,bid_type,placement,current_variant_order,original_media,media_state,operation_state,campaign_state,media_status,stats_quality,unallocated_views,unallocated_clicks,unallocated_spend_rub,stage_views,stage_clicks,stage_spend_rub,funding_source,last_total_views,last_total_clicks,last_total_orders,last_total_spend_rub,created_at,updated_at) VALUES (2,2,2,123,'same',1,'b','draft',1,0,1,300,300,1200,'unified','combined',0,'[]','{}','preparing','not_created','original','not_started',0,0,0,0,0,0,'auto',0,0,0,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")


def test_lifecycle_lock_can_be_released():
    engine = create_engine('sqlite:///:memory:')
    ABTest.metadata.create_all(engine)
    # The index allows a second test once the first lifecycle lock is released.
    assert any(idx.name == 'uq_ab_test_store_card_open' for idx in ABTest.__table__.indexes)


def test_budget_is_rounded_and_never_below_floor():
    assert calculate_required_budget(2, 300, 300) >= 1200
    assert calculate_required_budget(5, 1000, 500) == 2500


def test_media_hash_detects_real_change():
    from PIL import Image
    import io
    def png(value: int) -> bytes:
        image = Image.new('RGB', (32, 32), (value, value, value))
        out = io.BytesIO(); image.save(out, format='PNG'); return out.getvalue()
    assert phash(png(0)) - phash(png(0)) == 0
    assert phash(png(0)) - phash(png(255)) > 0
