import json
import os
import random
import time
from typing import Any, Dict, Optional

import pytest
from fixtures.common_types import TenantId, TimelineId
from fixtures.log_helper import log
from fixtures.neon_fixtures import NeonEnvBuilder, NeonPageserver, S3Scrubber
from fixtures.pageserver.common_types import parse_layer_file_name
from fixtures.pageserver.utils import (
    assert_prefix_empty,
    poll_for_remote_storage_iterations,
    tenant_delete_wait_completed,
    wait_for_upload_queue_empty,
)
from fixtures.remote_storage import LocalFsStorage, RemoteStorageKind, S3Storage
from fixtures.utils import wait_until
from fixtures.workload import Workload

# A tenant configuration that is convenient for generating uploads and deletions
# without a large amount of postgres traffic.
TENANT_CONF = {
    # small checkpointing and compaction targets to ensure we generate many upload operations
    "checkpoint_distance": f"{128 * 1024}",
    "compaction_target_size": f"{128 * 1024}",
    "compaction_threshold": "1",
    # no PITR horizon, we specify the horizon when we request on-demand GC
    "pitr_interval": "0s",
    # disable background compaction and GC. We invoke it manually when we want it to happen.
    "gc_period": "0s",
    "compaction_period": "0s",
    # create image layers eagerly, so that GC can remove some layers
    "image_creation_threshold": "1",
}


def evict_random_layers(
    rng: random.Random, pageserver: NeonPageserver, tenant_id: TenantId, timeline_id: TimelineId
):
    """
    Evict 50% of the layers on a pageserver
    """
    timeline_path = pageserver.timeline_dir(tenant_id, timeline_id)
    initial_local_layers = sorted(
        list(filter(lambda path: path.name != "metadata", timeline_path.glob("*")))
    )
    client = pageserver.http_client()
    for layer in initial_local_layers:
        if "ephemeral" in layer.name or "temp_download" in layer.name:
            continue

        layer_name = parse_layer_file_name(layer.name)

        if rng.choice([True, False]):
            log.info(f"Evicting layer {tenant_id}/{timeline_id} {layer_name.to_str()}")
            client.evict_layer(
                tenant_id=tenant_id, timeline_id=timeline_id, layer_name=layer_name.to_str()
            )


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_location_conf_churn(neon_env_builder: NeonEnvBuilder, seed: int):
    """
    Issue many location configuration changes, ensure that tenants
    remain readable & we don't get any unexpected errors.  We should
    have no ERROR in the log, and no 500s in the API.

    The location_config API is intentionally designed so that all destination
    states are valid, so that we may test it in this way: the API should always
    work as long as the tenant exists.
    """
    neon_env_builder.num_pageservers = 3
    neon_env_builder.enable_pageserver_remote_storage(
        remote_storage_kind=RemoteStorageKind.MOCK_S3,
    )
    env = neon_env_builder.init_start(initial_tenant_conf=TENANT_CONF)

    pageservers = env.pageservers
    list([p.http_client() for p in pageservers])
    tenant_id = env.initial_tenant
    timeline_id = env.initial_timeline

    for ps in env.pageservers:
        ps.allowed_errors.extend(
            [
                # We will make no effort to avoid stale attachments
                ".*Dropped remote consistent LSN updates.*",
                ".*Dropping stale deletions.*",
                # page_service_conn_main{peer_addr=[::1]:41176}: query handler for 'pagestream 3b19aec5038c796f64b430b30a555121 d07776761d44050b8aab511df1657d83' failed: Tenant 3b19aec5038c796f64b430b30a555121 not found
                ".*query handler.*Tenant.*not found.*",
                # page_service_conn_main{peer_addr=[::1]:45552}: query handler for 'pagestream 414ede7ad50f775a8e7d9ba0e43b9efc a43884be16f44b3626482b6981b2c745' failed: Tenant 414ede7ad50f775a8e7d9ba0e43b9efc is not active
                ".*query handler.*Tenant.*not active.*",
                # this shutdown case is logged at WARN severity by the time it bubbles up to logical size calculation code
                # WARN ...: initial size calculation failed: downloading failed, possibly for shutdown
                ".*downloading failed, possibly for shutdown",
                # {tenant_id=... timeline_id=...}:handle_pagerequests:handle_get_page_at_lsn_request{rel=1664/0/1260 blkno=0 req_lsn=0/149F0D8}: error reading relation or page version: Not found: will not become active.  Current state: Stopping\n'
                ".*page_service.*will not become active.*",
            ]
        )

        # these can happen, if we shutdown at a good time. to be fixed as part of #5172.
        message = ".*duplicated L1 layer layer=.*"
        ps.allowed_errors.append(message)

    workload = Workload(env, tenant_id, timeline_id)
    workload.init(env.pageservers[0].id)
    workload.write_rows(256, env.pageservers[0].id)

    # We use a fixed seed to make the test reproducible: we want a randomly
    # chosen order, but not to change the order every time we run the test.
    rng = random.Random(seed)

    initial_generation = 1
    last_state = {
        env.pageservers[0].id: ("AttachedSingle", initial_generation),
        env.pageservers[1].id: ("Detached", None),
        env.pageservers[2].id: ("Detached", None),
    }

    latest_attached = env.pageservers[0].id

    for _i in range(0, 64):
        # Pick a pageserver
        pageserver = rng.choice(env.pageservers)

        # Pick a pseudorandom state
        modes = [
            "AttachedSingle",
            "AttachedMulti",
            "AttachedStale",
            "Secondary",
            "Detached",
            "_Evictions",
            "_Restart",
        ]

        mode = rng.choice(modes)

        last_state_ps = last_state[pageserver.id]
        if mode == "_Evictions":
            if last_state_ps[0].startswith("Attached"):
                log.info(f"Action: evictions on pageserver {pageserver.id}")
                evict_random_layers(rng, pageserver, tenant_id, timeline_id)
            else:
                log.info(
                    f"Action: skipping evictions on pageserver {pageserver.id}, is not attached"
                )
        elif mode == "_Restart":
            log.info(f"Action: restarting pageserver {pageserver.id}")
            pageserver.stop()
            pageserver.start()
            if last_state_ps[0].startswith("Attached") and latest_attached == pageserver.id:
                # /re-attach call will bump generation: track that in our state in case we do an
                # "attach in same generation" operation later
                assert last_state_ps[1] is not None  # latest_attached == pageserfer.id implies this
                # The re-attach API increments generation by exactly one.
                new_generation = last_state_ps[1] + 1
                last_state[pageserver.id] = (last_state_ps[0], new_generation)
                tenants = pageserver.http_client().tenant_list()
                assert len(tenants) == 1
                assert tenants[0]["generation"] == new_generation

                log.info("Entering postgres...")
                workload.churn_rows(rng.randint(128, 256), pageserver.id)
                workload.validate(pageserver.id)
            elif last_state_ps[0].startswith("Attached"):
                # The `storage_controller` will only re-attach on startup when a pageserver was the
                # holder of the latest generation: otherwise the pageserver will revert to detached
                # state if it was running attached with a stale generation
                last_state[pageserver.id] = ("Detached", None)
        else:
            secondary_conf: Optional[Dict[str, Any]] = None
            if mode == "Secondary":
                secondary_conf = {"warm": rng.choice([True, False])}

            location_conf: Dict[str, Any] = {
                "mode": mode,
                "secondary_conf": secondary_conf,
                "tenant_conf": {},
            }

            log.info(f"Action: Configuring pageserver {pageserver.id} to {location_conf}")

            # Select a generation number
            if mode.startswith("Attached"):
                if last_state_ps[1] is not None:
                    if rng.choice([True, False]):
                        # Move between attached states, staying in the same generation
                        generation = last_state_ps[1]
                    else:
                        # Switch generations, while also jumping between attached states
                        generation = env.storage_controller.attach_hook_issue(
                            tenant_id, pageserver.id
                        )
                        latest_attached = pageserver.id
                else:
                    generation = env.storage_controller.attach_hook_issue(tenant_id, pageserver.id)
                    latest_attached = pageserver.id
            else:
                generation = None

            location_conf["generation"] = generation

            pageserver.tenant_location_configure(tenant_id, location_conf)
            last_state[pageserver.id] = (mode, generation)

            if mode.startswith("Attached"):
                # This is a basic test: we are validating that he endpoint works properly _between_
                # configuration changes.  A stronger test would be to validate that clients see
                # no errors while we are making the changes.
                workload.churn_rows(
                    rng.randint(128, 256), pageserver.id, upload=mode != "AttachedStale"
                )
                workload.validate(pageserver.id)

    # Attach all pageservers
    for ps in env.pageservers:
        location_conf = {"mode": "AttachedMulti", "secondary_conf": None, "tenant_conf": {}}
        ps.tenant_location_configure(tenant_id, location_conf)

    # Confirm that all are readable
    for ps in env.pageservers:
        workload.validate(ps.id)

    # Detach all pageservers
    for ps in env.pageservers:
        location_conf = {"mode": "Detached", "secondary_conf": None, "tenant_conf": {}}
        ps.tenant_location_configure(tenant_id, location_conf)

    # Confirm that all local disk state was removed on detach
    # TODO


def test_live_migration(neon_env_builder: NeonEnvBuilder):
    """
    Test the sequence of location states that are used in a live migration.
    """
    neon_env_builder.num_pageservers = 2
    remote_storage_kind = RemoteStorageKind.MOCK_S3
    neon_env_builder.enable_pageserver_remote_storage(remote_storage_kind=remote_storage_kind)
    env = neon_env_builder.init_start(initial_tenant_conf=TENANT_CONF)

    tenant_id = env.initial_tenant
    timeline_id = env.initial_timeline

    pageserver_a = env.pageservers[0]
    pageserver_b = env.pageservers[1]

    initial_generation = 1

    workload = Workload(env, tenant_id, timeline_id)
    workload.init(env.pageservers[0].id)
    workload.write_rows(256, env.pageservers[0].id)

    # Make the destination a secondary location
    pageserver_b.tenant_location_configure(
        tenant_id,
        {
            "mode": "Secondary",
            "secondary_conf": {"warm": True},
            "tenant_conf": {},
        },
    )

    workload.churn_rows(64, pageserver_a.id, upload=False)

    # Set origin attachment to stale
    log.info("Setting origin to AttachedStale")
    pageserver_a.tenant_location_configure(
        tenant_id,
        {
            "mode": "AttachedStale",
            "secondary_conf": None,
            "tenant_conf": {},
            "generation": initial_generation,
        },
        flush_ms=5000,
    )

    # Encourage the new location to download while still in secondary mode
    pageserver_b.http_client().tenant_secondary_download(tenant_id)

    migrated_generation = env.storage_controller.attach_hook_issue(tenant_id, pageserver_b.id)
    log.info(f"Acquired generation {migrated_generation} for destination pageserver")
    assert migrated_generation == initial_generation + 1

    # Writes and reads still work in AttachedStale.
    workload.validate(pageserver_a.id)

    # Generate some more dirty writes: we expect the origin to ingest WAL in
    # in AttachedStale
    workload.churn_rows(64, pageserver_a.id, upload=False)
    workload.validate(pageserver_a.id)

    # Attach the destination
    log.info("Setting destination to AttachedMulti")
    pageserver_b.tenant_location_configure(
        tenant_id,
        {
            "mode": "AttachedMulti",
            "secondary_conf": None,
            "tenant_conf": {},
            "generation": migrated_generation,
        },
    )

    # Wait for destination LSN to catch up with origin
    origin_lsn = pageserver_a.http_client().timeline_detail(tenant_id, timeline_id)[
        "last_record_lsn"
    ]

    def caught_up():
        destination_lsn = pageserver_b.http_client().timeline_detail(tenant_id, timeline_id)[
            "last_record_lsn"
        ]
        log.info(
            f"Waiting for LSN to catch up: origin {origin_lsn} vs destination {destination_lsn}"
        )
        assert destination_lsn >= origin_lsn

    wait_until(100, 0.1, caught_up)

    # The destination should accept writes
    workload.churn_rows(64, pageserver_b.id)

    # Dual attached: both are readable.
    workload.validate(pageserver_a.id)
    workload.validate(pageserver_b.id)

    # Revert the origin to secondary
    log.info("Setting origin to Secondary")
    pageserver_a.tenant_location_configure(
        tenant_id,
        {
            "mode": "Secondary",
            "secondary_conf": {"warm": True},
            "tenant_conf": {},
        },
    )

    workload.churn_rows(64, pageserver_b.id)

    # Put the destination into final state
    pageserver_b.tenant_location_configure(
        tenant_id,
        {
            "mode": "AttachedSingle",
            "secondary_conf": None,
            "tenant_conf": {},
            "generation": migrated_generation,
        },
    )

    workload.churn_rows(64, pageserver_b.id)
    workload.validate(pageserver_b.id)
    del workload

    # Check that deletion works properly on a tenant that was live-migrated
    # (reproduce https://github.com/neondatabase/neon/issues/6802)
    iterations = poll_for_remote_storage_iterations(remote_storage_kind)
    tenant_delete_wait_completed(pageserver_b.http_client(), tenant_id, iterations)


def test_heatmap_uploads(neon_env_builder: NeonEnvBuilder):
    """
    Test the sequence of location states that are used in a live migration.
    """
    env = neon_env_builder.init_start()  # initial_tenant_conf=TENANT_CONF)
    assert isinstance(env.pageserver_remote_storage, LocalFsStorage)

    tenant_id = env.initial_tenant
    timeline_id = env.initial_timeline

    # Write some data so that we have some layers
    workload = Workload(env, tenant_id, timeline_id)
    workload.init(env.pageservers[0].id)

    # Write some layers and upload a heatmap
    workload.write_rows(256, env.pageservers[0].id)
    env.pageserver.http_client().tenant_heatmap_upload(tenant_id)

    def validate_heatmap(heatmap):
        assert len(heatmap["timelines"]) == 1
        assert heatmap["timelines"][0]["timeline_id"] == str(timeline_id)
        assert len(heatmap["timelines"][0]["layers"]) > 0
        layers = heatmap["timelines"][0]["layers"]

        # Each layer appears at most once
        assert len(set(layer["name"] for layer in layers)) == len(layers)

    # Download and inspect the heatmap that the pageserver uploaded
    heatmap_first = env.pageserver_remote_storage.heatmap_content(tenant_id)
    log.info(f"Read back heatmap: {heatmap_first}")
    validate_heatmap(heatmap_first)

    # Do some more I/O to generate more layers
    workload.churn_rows(64, env.pageservers[0].id)
    env.pageserver.http_client().tenant_heatmap_upload(tenant_id)

    # Ensure that another heatmap upload includes the new layers
    heatmap_second = env.pageserver_remote_storage.heatmap_content(tenant_id)
    log.info(f"Read back heatmap: {heatmap_second}")
    assert heatmap_second != heatmap_first
    validate_heatmap(heatmap_second)


def test_secondary_downloads(neon_env_builder: NeonEnvBuilder):
    """
    Test the overall data flow in secondary mode:
     - Heatmap uploads from the attached location
     - Heatmap & layer downloads from the secondary location
     - Eviction of layers on the attached location results in deletion
       on the secondary location as well.
    """

    # For debug of https://github.com/neondatabase/neon/issues/6966
    neon_env_builder.rust_log_override = "DEBUG"

    neon_env_builder.num_pageservers = 2
    neon_env_builder.enable_pageserver_remote_storage(
        remote_storage_kind=RemoteStorageKind.MOCK_S3,
    )
    env = neon_env_builder.init_start(initial_tenant_conf=TENANT_CONF)
    assert env.storage_controller is not None
    assert isinstance(env.pageserver_remote_storage, S3Storage)  # Satisfy linter

    tenant_id = env.initial_tenant
    timeline_id = env.initial_timeline

    ps_attached = env.pageservers[0]
    ps_secondary = env.pageservers[1]

    workload = Workload(env, tenant_id, timeline_id)
    workload.init(env.pageservers[0].id)
    workload.write_rows(256, ps_attached.id)

    # Configure a secondary location
    log.info("Setting up secondary location...")
    ps_secondary.tenant_location_configure(
        tenant_id,
        {
            "mode": "Secondary",
            "secondary_conf": {"warm": True},
            "tenant_conf": {},
        },
    )
    readback_conf = ps_secondary.read_tenant_location_conf(tenant_id)
    log.info(f"Read back conf: {readback_conf}")

    # Explicit upload/download cycle
    # ==============================
    log.info("Synchronizing after initial write...")
    ps_attached.http_client().tenant_heatmap_upload(tenant_id)

    # Ensure that everything which appears in the heatmap is also present in S3: heatmap writers
    # are allowed to upload heatmaps that reference layers which are only enqueued for upload
    wait_for_upload_queue_empty(ps_attached.http_client(), tenant_id, timeline_id)

    ps_secondary.http_client().tenant_secondary_download(tenant_id)

    assert ps_attached.list_layers(tenant_id, timeline_id) == ps_secondary.list_layers(
        tenant_id, timeline_id
    )

    # Make changes on attached pageserver, check secondary downloads them
    # ===================================================================
    log.info("Synchronizing after subsequent write...")
    workload.churn_rows(128, ps_attached.id)

    ps_attached.http_client().tenant_heatmap_upload(tenant_id)

    # Ensure that everything which appears in the heatmap is also present in S3: heatmap writers
    # are allowed to upload heatmaps that reference layers which are only enqueued for upload
    wait_for_upload_queue_empty(ps_attached.http_client(), tenant_id, timeline_id)

    ps_secondary.http_client().tenant_secondary_download(tenant_id)

    try:
        assert ps_attached.list_layers(tenant_id, timeline_id) == ps_secondary.list_layers(
            tenant_id, timeline_id
        )
    except:
        # Do a full listing of the secondary location on errors, to help debug of
        # https://github.com/neondatabase/neon/issues/6966
        timeline_path = ps_secondary.timeline_dir(tenant_id, timeline_id)
        for path, _dirs, files in os.walk(timeline_path):
            for f in files:
                log.info(f"Secondary file: {os.path.join(path, f)}")

        raise

    # FIXME: this sleep is needed to avoid on-demand promotion of the layers we evict, while
    # walreceiver is still doing something.
    import time

    time.sleep(5)

    # Do evictions on attached pageserver, check secondary follows along
    # ==================================================================
    try:
        log.info("Evicting a layer...")
        layer_to_evict = ps_attached.list_layers(tenant_id, timeline_id)[0]
        some_other_layer = ps_attached.list_layers(tenant_id, timeline_id)[1]
        log.info(f"Victim layer: {layer_to_evict.name}")
        ps_attached.http_client().evict_layer(
            tenant_id, timeline_id, layer_name=layer_to_evict.name
        )

        log.info("Synchronizing after eviction...")
        ps_attached.http_client().tenant_heatmap_upload(tenant_id)
        heatmap_after_eviction = env.pageserver_remote_storage.heatmap_content(tenant_id)
        heatmap_layers = set(
            layer["name"] for layer in heatmap_after_eviction["timelines"][0]["layers"]
        )
        assert layer_to_evict.name not in heatmap_layers
        assert parse_layer_file_name(some_other_layer.name).to_str() in heatmap_layers

        ps_secondary.http_client().tenant_secondary_download(tenant_id)

        assert layer_to_evict not in ps_attached.list_layers(tenant_id, timeline_id)
        assert ps_attached.list_layers(tenant_id, timeline_id) == ps_secondary.list_layers(
            tenant_id, timeline_id
        )
    except:
        # On assertion failures, log some details to help with debugging
        heatmap = env.pageserver_remote_storage.heatmap_content(tenant_id)
        log.warn(f"heatmap contents: {json.dumps(heatmap,indent=2)}")
        raise

    # Scrub the remote storage
    # ========================
    # This confirms that the scrubber isn't upset by the presence of the heatmap
    S3Scrubber(neon_env_builder).scan_metadata()

    # Detach secondary and delete tenant
    # ===================================
    # This confirms that the heatmap gets cleaned up as well as other normal content.
    log.info("Detaching secondary location...")
    ps_secondary.tenant_location_configure(
        tenant_id,
        {
            "mode": "Detached",
            "secondary_conf": None,
            "tenant_conf": {},
        },
    )

    log.info("Deleting tenant...")
    tenant_delete_wait_completed(ps_attached.http_client(), tenant_id, 10)

    assert_prefix_empty(
        neon_env_builder.pageserver_remote_storage,
        prefix="/".join(
            (
                "tenants",
                str(tenant_id),
            )
        ),
    )


def test_secondary_background_downloads(neon_env_builder: NeonEnvBuilder):
    """
    Slow test that runs in realtime, checks that the background scheduling of secondary
    downloads happens as expected.
    """
    neon_env_builder.num_pageservers = 2
    env = neon_env_builder.init_configs()
    env.start()

    # Create this many tenants, each with two timelines
    tenant_count = 4
    tenant_timelines = {}

    # This mirrors a constant in `downloader.rs`
    default_download_period_secs = 60

    # The upload period, which will also be the download once the secondary has seen its first heatmap
    upload_period_secs = 20

    for _i in range(0, tenant_count):
        tenant_id = TenantId.generate()
        timeline_a = TimelineId.generate()
        timeline_b = TimelineId.generate()
        env.neon_cli.create_tenant(
            tenant_id,
            timeline_a,
            placement_policy='{"Attached":1}',
            # Run with a low heatmap period so that we can avoid having to do synthetic API calls
            # to trigger the upload promptly.
            conf={"heatmap_period": f"{upload_period_secs}s"},
        )
        env.neon_cli.create_timeline("main2", tenant_id, timeline_b)

        tenant_timelines[tenant_id] = [timeline_a, timeline_b]

    t_start = time.time()

    # Wait long enough that the background downloads should happen; we expect all the inital layers
    # of all the initial timelines to show up on the secondary location of each tenant.
    time.sleep(default_download_period_secs * 1.5)

    for tenant_id, timelines in tenant_timelines.items():
        attached_to_id = env.storage_controller.locate(tenant_id)[0]["node_id"]
        ps_attached = env.get_pageserver(attached_to_id)
        # We only have two: the other one must be secondary
        ps_secondary = next(p for p in env.pageservers if p != ps_attached)

        for timeline_id in timelines:
            log.info(f"Checking for secondary timeline {timeline_id} on node {ps_secondary.id}")
            # One or more layers should be present for all timelines
            assert ps_secondary.list_layers(tenant_id, timeline_id)

        # Delete the second timeline: this should be reflected later on the secondary
        env.storage_controller.pageserver_api().timeline_delete(tenant_id, timelines[1])

    # Wait long enough for the secondary locations to see the deletion: 2x period plus a grace factor
    time.sleep(upload_period_secs * 2.5)

    for tenant_id, timelines in tenant_timelines.items():
        attached_to_id = env.storage_controller.locate(tenant_id)[0]["node_id"]
        ps_attached = env.get_pageserver(attached_to_id)
        # We only have two: the other one must be secondary
        ps_secondary = next(p for p in env.pageservers if p != ps_attached)

        # This one was not deleted
        assert ps_secondary.list_layers(tenant_id, timelines[0])

        # This one was deleted
        log.info(
            f"Checking for secondary timeline deletion {tenant_id}/{timeline_id} on node {ps_secondary.id}"
        )
        assert not ps_secondary.list_layers(tenant_id, timelines[1])

    t_end = time.time()

    # Measure how many heatmap downloads we did in total: this checks that we succeeded with
    # proper scheduling, and not some bug that just runs downloads in a loop.
    total_heatmap_downloads = 0
    for ps in env.pageservers:
        v = ps.http_client().get_metric_value("pageserver_secondary_download_heatmap_total")
        assert v is not None
        total_heatmap_downloads += int(v)

    download_rate = (total_heatmap_downloads / tenant_count) / (t_end - t_start)

    expect_download_rate = 1.0 / upload_period_secs
    log.info(f"Download rate: {download_rate * 60}/min vs expected {expect_download_rate * 60}/min")

    assert download_rate < expect_download_rate * 2


@pytest.mark.skipif(os.environ.get("BUILD_TYPE") == "debug", reason="only run with release build")
@pytest.mark.parametrize("via_controller", [True, False])
def test_slow_secondary_downloads(neon_env_builder: NeonEnvBuilder, via_controller: bool):
    """
    Test use of secondary download API for slow downloads, where slow means either a healthy
    system with a large capacity shard, or some unhealthy remote storage.

    The download API is meant to respect a client-supplied time limit, and return 200 or 202
    selectively based on whether the download completed.
    """
    neon_env_builder.num_pageservers = 2
    neon_env_builder.enable_pageserver_remote_storage(
        remote_storage_kind=RemoteStorageKind.MOCK_S3,
    )
    env = neon_env_builder.init_start(initial_tenant_conf=TENANT_CONF)

    tenant_id = TenantId.generate()
    timeline_id = TimelineId.generate()

    env.neon_cli.create_tenant(
        tenant_id, timeline_id, conf=TENANT_CONF, placement_policy='{"Attached":1}'
    )

    attached_to_id = env.storage_controller.locate(tenant_id)[0]["node_id"]
    ps_attached = env.get_pageserver(attached_to_id)
    ps_secondary = next(p for p in env.pageservers if p != ps_attached)

    # Generate a bunch of small layers (we will apply a slowdown failpoint that works on a per-layer basis)
    workload = Workload(env, tenant_id, timeline_id)
    workload.init()
    workload.write_rows(128)
    ps_attached.http_client().timeline_checkpoint(tenant_id, timeline_id)
    workload.write_rows(128)
    ps_attached.http_client().timeline_checkpoint(tenant_id, timeline_id)
    workload.write_rows(128)
    ps_attached.http_client().timeline_checkpoint(tenant_id, timeline_id)
    workload.write_rows(128)
    ps_attached.http_client().timeline_checkpoint(tenant_id, timeline_id)

    # Expect lots of layers
    assert len(ps_attached.list_layers(tenant_id, timeline_id)) > 10

    # Simulate large data by making layer downloads artifically slow
    for ps in env.pageservers:
        ps.http_client().configure_failpoints([("secondary-layer-download-sleep", "return(1000)")])

    # Upload a heatmap, so that secondaries have something to download
    ps_attached.http_client().tenant_heatmap_upload(tenant_id)

    if via_controller:
        http_client = env.storage_controller.pageserver_api()
        http_client.tenant_location_conf(
            tenant_id,
            {
                "mode": "Secondary",
                "secondary_conf": {"warm": True},
                "tenant_conf": {},
                "generation": None,
            },
        )
    else:
        http_client = ps_secondary.http_client()

    # This has no chance to succeed: we have lots of layers and each one takes at least 1000ms
    (status, progress_1) = http_client.tenant_secondary_download(tenant_id, wait_ms=4000)
    assert status == 202
    assert progress_1["heatmap_mtime"] is not None
    assert progress_1["layers_downloaded"] > 0
    assert progress_1["bytes_downloaded"] > 0
    assert progress_1["layers_total"] > progress_1["layers_downloaded"]
    assert progress_1["bytes_total"] > progress_1["bytes_downloaded"]

    # Multiple polls should work: use a shorter wait period this time
    (status, progress_2) = http_client.tenant_secondary_download(tenant_id, wait_ms=1000)
    assert status == 202
    assert progress_2["heatmap_mtime"] is not None
    assert progress_2["layers_downloaded"] > 0
    assert progress_2["bytes_downloaded"] > 0
    assert progress_2["layers_total"] > progress_2["layers_downloaded"]
    assert progress_2["bytes_total"] > progress_2["bytes_downloaded"]

    # Progress should be >= the first poll: this can only go backward if we see a new heatmap,
    # and the heatmap period on the attached node is much longer than the runtime of this test, so no
    # new heatmap should have been uploaded.
    assert progress_2["layers_downloaded"] >= progress_1["layers_downloaded"]
    assert progress_2["bytes_downloaded"] >= progress_1["bytes_downloaded"]
    assert progress_2["layers_total"] == progress_1["layers_total"]
    assert progress_2["bytes_total"] == progress_1["bytes_total"]

    # Make downloads fast again: when the download completes within this last request, we
    # get a 200 instead of a 202
    for ps in env.pageservers:
        ps.http_client().configure_failpoints([("secondary-layer-download-sleep", "off")])
    (status, progress_3) = http_client.tenant_secondary_download(tenant_id, wait_ms=20000)
    assert status == 200
    assert progress_3["heatmap_mtime"] is not None
    assert progress_3["layers_total"] == progress_3["layers_downloaded"]
    assert progress_3["bytes_total"] == progress_3["bytes_downloaded"]
