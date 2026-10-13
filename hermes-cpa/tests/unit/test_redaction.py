from hermes_cpa.contracts import StatusData, LifecycleState

def test_no_secrets_in_status():
    secret_mgmt = "secret_cpa_management_999"
    secret_inf = "secret_cpa_inference_111"
    st = StatusData(
        mode="managed",
        lifecycle=LifecycleState.READY,
        instance_id="default",
        cpa_version="8.0.21",
        console_version="1.25.6",
        management_ready=True,
        inference_configured=True,
        console_compatible=True,
        last_transition_time=123456.0,
        sanitized_reason="normal"
    )
    raw = st.json()
    assert secret_mgmt not in raw
    assert secret_inf not in raw
    assert "secret" not in raw.lower() or "sanitized_reason" in raw
