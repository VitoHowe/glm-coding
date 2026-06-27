<script setup lang="ts">
import QrPreview from "./QrPreview.vue";
import TicketPoolEditor from "./TicketPoolEditor.vue";
import { zhCN as copy } from "../locales/zhCN";
import type {
    AccountDetailResponse,
    PaymentTaskRecord,
    ProductOffer,
} from "../types/api";

defineProps<{
    details: AccountDetailResponse[];
    loading: boolean;
    actionKey: string;
}>();

const emit = defineEmits<{
    openContext: [detail: AccountDetailResponse];
    selectProduct: [accountId: string, productId: string];
    updatePreviewConcurrency: [accountId: string, value: number];
    updateStartTime: [accountId: string, time: string];
    updateTicketStartTime: [accountId: string, time: string];
    sync: [accountId: string];
    delete: [accountId: string];
    run: [accountId: string];
    startStockMonitor: [accountId: string];
    stopStockMonitor: [accountId: string];
    pause: [accountId: string];
    updateTicketPool: [accountId: string, size: number, drainIntervalMs: number];
    clearTicketPool: [accountId: string];
}>();

function latestTask(detail: AccountDetailResponse): PaymentTaskRecord | null {
    return detail.tasks?.[0] || null;
}

function productForTask(detail: AccountDetailResponse): ProductOffer | null {
    const task = latestTask(detail);
    if (!task) {
        return null;
    }
    return (
        detail.session.products?.find(
            (product) => product.product_id === task.product_id,
        ) || null
    );
}

function isRunning(detail: AccountDetailResponse) {
    return ["running", "pause_requested"].includes(
        String(detail.account.last_schedule_status || "").toLowerCase(),
    );
}

function isScheduleSuccess(detail: AccountDetailResponse) {
    return (
        String(detail.account.last_schedule_status || "").toLowerCase() ===
        "success"
    );
}

function isStockMonitoring(detail: AccountDetailResponse) {
    return Boolean(detail.account.stock_monitor_enabled);
}

function startTimeStateText(detail: AccountDetailResponse) {
    return detail.account.preview_concurrency_time || copy.table.notConfigured;
}

function productOptions(detail: AccountDetailResponse) {
    return (detail.session.products || []).map((product) => ({
        label: `${product.product_name} / ${product.unit || "-"}`,
        value: product.product_id,
        disabled: Boolean(product.sold_out || product.forbidden),
    }));
}

function selectedProduct(detail: AccountDetailResponse) {
    return detail.session.selected_product_id || null;
}

function previewConcurrencyValue(detail: AccountDetailResponse) {
    return Math.max(
        1,
        Math.min(4, Number(detail.account.preview_concurrency || 1)),
    );
}

function updatePreviewConcurrency(
    accountId: string,
    raw: string | number | null,
) {
    const value = Math.max(1, Math.min(4, Number(raw || 1)));
    emit("updatePreviewConcurrency", accountId, value);
}

function startTimeValue(detail: AccountDetailResponse) {
    return detail.account.preview_concurrency_time || "";
}

function displayMode(detail: AccountDetailResponse) {
    return detail.session.purchase_mode === "upgrade"
        ? copy.table.modes.upgrade
        : copy.table.modes.newPurchase;
}

function actionLoading(key: string, accountId: string, actionKey: string) {
    return actionKey === `${key}:${accountId}`;
}

function ticketPoolSize(detail: AccountDetailResponse) {
    return Math.max(0, Number(detail.account.ticket_pool_size || 0));
}

function ticketPoolDrainIntervalMs(detail: AccountDetailResponse) {
    return Math.max(
        0,
        Math.min(
            10000,
            Number(detail.account.ticket_pool_drain_interval_ms || 0),
        ),
    );
}

function ticketPoolCollected(detail: AccountDetailResponse) {
    const pool = detail.session.ticket_pool || [];
    return pool.filter((e) => !e.used).length;
}

function ticketPoolTarget(detail: AccountDetailResponse) {
    return ticketPoolSize(detail);
}

function onTicketPoolUpdate(
    accountId: string,
    enabled: boolean,
    size: number,
    drainIntervalMs: number,
) {
    // enabled=false → size=0 (pool off), enabled=true → size=N (pool on)
    emit("updateTicketPool", accountId, enabled ? size : 0, drainIntervalMs);
}
</script>

<template>
    <n-card class="table-panel" :bordered="false">
        <template #header>
            <div class="panel-title">
                <div>
                    <span>{{ copy.table.title }}</span>
                    <small>{{ copy.table.subtitle(details.length) }}</small>
                </div>
            </div>
        </template>

        <n-spin :show="loading" class="table-spin">
            <div
                class="ops-table"
                role="table"
                :aria-label="copy.table.regionLabel"
                tabindex="0"
            >
                <div class="ops-table-head" role="row">
                    <span role="columnheader">{{
                        copy.table.columns.account
                    }}</span>
                    <span role="columnheader">{{
                        copy.table.columns.product
                    }}</span>
                    <span role="columnheader">{{
                        copy.table.columns.schedule
                    }}</span>
                    <span role="columnheader">{{
                        copy.table.columns.status
                    }}</span>
                    <span role="columnheader">{{
                        copy.table.columns.latestQr
                    }}</span>
                    <span role="columnheader">{{
                        copy.table.columns.actions
                    }}</span>
                </div>

                <div v-if="details.length === 0" class="ops-empty">
                    <n-empty :description="copy.table.empty" />
                </div>

                <article
                    v-for="detail in details"
                    :key="detail.account.id"
                    class="ops-row"
                    role="row"
                >
                    <section class="ops-cell account-cell" role="cell">
                        <button
                            class="link-button"
                            type="button"
                            @click="emit('openContext', detail)"
                        >
                            {{ detail.account.label }}
                        </button>
                        <span
                            >{{ displayMode(detail) }} /
                            {{
                                detail.account.browser_impersonate ||
                                copy.table.browserPending
                            }}</span
                        >
                    </section>

                    <section class="ops-cell product-cell" role="cell">
                        <n-select
                            :value="selectedProduct(detail)"
                            :options="productOptions(detail)"
                            :placeholder="copy.table.selectProduct"
                            clearable
                            size="large"
                            @update:value="
                                emit(
                                    'selectProduct',
                                    detail.account.id,
                                    String($event || ''),
                                )
                            "
                        />
                    </section>

                    <section class="ops-cell config-cell" role="cell">
                        <div class="execution-config">
                            <div class="execution-row">
                                <span class="config-label">{{
                                    copy.table.executionGroups.startTime
                                }}</span>
                                <input
                                    class="time-input"
                                    type="time"
                                    step="1"
                                    :value="startTimeValue(detail)"
                                    :aria-label="copy.table.startTimeLabel"
                                    @change="
                                        (e: Event) =>
                                            emit(
                                                'updateStartTime',
                                                detail.account.id,
                                                (e.target as HTMLInputElement).value || '',
                                            )
                                    "
                                />
                            </div>
                            <div class="execution-row">
                                <span class="config-label">{{
                                    copy.table.executionGroups.preview
                                }}</span>
                                <label class="preview-race-control">
                                    <span>{{
                                        copy.table.previewConcurrencyShort
                                    }}</span>
                                    <select
                                        :value="
                                            previewConcurrencyValue(detail)
                                        "
                                        :aria-label="
                                            copy.table.previewConcurrency
                                        "
                                        @change="
                                            updatePreviewConcurrency(
                                                detail.account.id,
                                                (
                                                    $event.target as HTMLSelectElement
                                                ).value,
                                            )
                                        "
                                    >
                                        <option :value="1">1</option>
                                        <option :value="2">2</option>
                                        <option :value="3">3</option>
                                        <option :value="4">4</option>
                                    </select>
                                </label>
                            </div>
                            <div class="execution-row">
                                <span class="config-label">{{
                                    copy.table.executionGroups.ticket
                                }}</span>
                                <TicketPoolEditor
                                    :account-id="detail.account.id"
                                    :enabled="ticketPoolSize(detail) > 0"
                                    :size="ticketPoolSize(detail)"
                                    :drain-interval-ms="
                                        ticketPoolDrainIntervalMs(detail)
                                    "
                                    :collected="ticketPoolCollected(detail)"
                                    :target="ticketPoolTarget(detail)"
                                    :ticket-start-time="
                                        detail.account.ticket_pool_start_time || ''
                                    "
                                    @update="onTicketPoolUpdate"
                                    @update-ticket-start-time="
                                        (id, time) =>
                                            emit('updateTicketStartTime', id, time)
                                    "
                                    @clear-pool="
                                        emit('clearTicketPool', $event)
                                    "
                                />
                            </div>
                        </div>
                    </section>

                    <section class="ops-cell status-cell" role="cell">
                        <n-tag
                            :type="
                                detail.account.account_status === 'valid'
                                    ? 'success'
                                    : 'warning'
                            "
                            round
                        >
                            {{
                                detail.account.account_status ||
                                copy.table.unchecked
                            }}
                        </n-tag>
                        <small>{{
                            detail.account.last_schedule_status ||
                            detail.account.account_status_message ||
                            copy.table.noRecentEvent
                        }}</small>
                        <small
                            v-if="detail.account.account_status_message"
                            class="account-state-line"
                        >
                            {{ detail.account.account_status_message }}
                        </small>
                        <small
                            v-if="detail.account.stock_monitor_last_message"
                            class="schedule-state-line"
                        >
                            库存:
                            {{ detail.account.stock_monitor_last_message }}
                        </small>
                        <small class="schedule-state-line"
                            >{{ copy.table.startTimeLabel }}:
                            {{ startTimeStateText(detail) }}</small
                        >
                        <small
                            v-if="
                                isScheduleSuccess(detail) &&
                                latestTask(detail)?.biz_id
                            "
                            class="mono-line"
                        >
                            {{ copy.table.bizId }}:
                            {{ latestTask(detail)?.biz_id }}
                        </small>
                    </section>

                    <section class="ops-cell qr-column" role="cell">
                        <QrPreview
                            :task="latestTask(detail)"
                            :product-unit="productForTask(detail)?.unit"
                        />
                    </section>

                    <section class="ops-cell actions-cell" role="cell">
                        <n-button
                            type="primary"
                            :loading="
                                actionLoading(
                                    isRunning(detail) ? 'pause' : 'run',
                                    detail.account.id,
                                    actionKey,
                                )
                            "
                            @click="
                                isRunning(detail)
                                    ? emit('pause', detail.account.id)
                                    : emit('run', detail.account.id)
                            "
                        >
                            {{
                                isRunning(detail)
                                    ? copy.table.pause
                                    : copy.table.run
                            }}
                        </n-button>
                        <n-button
                            secondary
                            :type="isStockMonitoring(detail) ? 'warning' : 'default'"
                            :loading="
                                actionLoading(
                                    'stock',
                                    detail.account.id,
                                    actionKey,
                                )
                            "
                            @click="
                                isStockMonitoring(detail)
                                    ? emit('stopStockMonitor', detail.account.id)
                                    : emit('startStockMonitor', detail.account.id)
                            "
                        >
                            {{
                                isStockMonitoring(detail)
                                    ? copy.table.stopStockMonitor
                                    : copy.table.stockMonitor
                            }}
                        </n-button>
                        <n-button
                            secondary
                            :loading="
                                actionLoading(
                                    'sync',
                                    detail.account.id,
                                    actionKey,
                                )
                            "
                            @click="emit('sync', detail.account.id)"
                        >
                            {{ copy.table.syncFingerprint }}
                        </n-button>
                        <n-popconfirm
                            @positive-click="emit('delete', detail.account.id)"
                        >
                            <template #trigger>
                                <n-button
                                    quaternary
                                    type="error"
                                    :loading="
                                        actionLoading(
                                            'delete',
                                            detail.account.id,
                                            actionKey,
                                        )
                                    "
                                >
                                    {{ copy.table.delete }}
                                </n-button>
                            </template>
                            {{ copy.table.deleteConfirm }}
                        </n-popconfirm>
                    </section>
                </article>
            </div>
        </n-spin>
    </n-card>
</template>
