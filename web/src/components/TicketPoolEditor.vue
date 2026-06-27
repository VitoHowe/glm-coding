<script setup lang="ts">
import { zhCN as copy } from "../locales/zhCN";

const props = defineProps<{
    accountId: string;
    enabled: boolean;
    size: number;
    drainIntervalMs: number;
    collected: number;
    target: number;
    ticketStartTime: string;
}>();

const emit = defineEmits<{
    update: [accountId: string, enabled: boolean, size: number, drainIntervalMs: number];
    updateTicketStartTime: [accountId: string, time: string];
    clearPool: [accountId: string];
}>();

const DEFAULT_POOL_SIZE = 5;

function toggleEnabled(enabled: boolean) {
    const nextSize = enabled
        ? Math.max(props.size || 0, DEFAULT_POOL_SIZE)
        : 0;
    emit("update", props.accountId, enabled, nextSize, props.drainIntervalMs);
}

function updateSize(event: Event) {
    const raw = parseInt((event.target as HTMLInputElement).value, 10);
    const size = Math.max(1, Math.min(50, isNaN(raw) ? DEFAULT_POOL_SIZE : raw));
    emit("update", props.accountId, true, size, props.drainIntervalMs);
}

function updateDrainInterval(event: Event) {
    const raw = parseInt((event.target as HTMLInputElement).value, 10);
    const intervalMs = Math.max(0, Math.min(10000, isNaN(raw) ? 0 : raw));
    emit("update", props.accountId, true, props.size || DEFAULT_POOL_SIZE, intervalMs);
}
</script>

<template>
    <div class="ticket-pool-editor" :class="{ 'is-disabled': !enabled }">
        <div class="ticket-pool-main">
            <n-switch
                :value="enabled"
                :aria-label="copy.ticketPool.enableLabel"
                @update:value="toggleEnabled"
            />
            <span class="ticket-pool-state">{{
                enabled ? copy.ticketPool.on : copy.ticketPool.off
            }}</span>
            <span
                v-if="enabled && target > 0"
                :class="[
                    'ticket-pool-progress',
                    collected >= target ? 'is-ready' : 'is-filling',
                ]"
            >
                {{ copy.ticketPool.status(collected, target) }}
            </span>
        </div>
        <div v-if="enabled" class="ticket-pool-fields">
            <label class="compact-number-field pool-size-field">
                <span>{{ copy.ticketPool.sizeShort }}</span>
                <input
                    class="pool-size-input"
                    type="number"
                    min="1"
                    max="50"
                    step="1"
                    :value="size"
                    :title="copy.ticketPool.sizeHint"
                    :aria-label="copy.ticketPool.sizeLabel"
                    :disabled="collected > 0"
                    @change="updateSize"
                />
            </label>
            <label class="compact-number-field pool-interval-field">
                <span>{{ copy.ticketPool.intervalShort }}</span>
                <input
                    class="pool-interval-input"
                    type="number"
                    min="0"
                    max="10000"
                    step="50"
                    :value="drainIntervalMs"
                    :title="copy.ticketPool.intervalHint"
                    :aria-label="copy.ticketPool.intervalLabel"
                    @change="updateDrainInterval"
                />
            </label>
            <label class="compact-number-field pool-start-field">
                <span>{{ copy.ticketPool.ticketStartTimeLabel }}</span>
                <input
                    class="time-input"
                    type="time"
                    step="1"
                    :value="ticketStartTime"
                    :aria-label="copy.ticketPool.ticketStartTimeLabel"
                    @change="
                        (e: Event) => emit('updateTicketStartTime', accountId, (e.target as HTMLInputElement).value || '')
                    "
                />
            </label>
            <n-button
                v-if="collected > 0"
                class="ticket-pool-clear"
                size="tiny"
                type="warning"
                ghost
                @click="emit('clearPool', accountId)"
            >
                {{ copy.ticketPool.clearPool }}
            </n-button>
        </div>
    </div>
</template>
