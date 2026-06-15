export const zhCN = {
  app: {
    title: "GLM Desk",
    eyebrow: "本地运营控制台",
    description: "集中管理账号编排、支付二维码、定时任务和上下文排查。",
    transportPending: "传输状态待确认",
    primaryActionsLabel: "主要操作",
    commandCenterLabel: "GLM Desk 控制中心",
    viewLogs: "查看日志",
    logsTitle: "当日运行日志",
    logsToday: "今日日志",
    refreshLogs: "刷新日志",
    noLogs: "今天暂无运行日志",
    refresh: "刷新列表",
    importAccount: "导入账号"
  },
  stats: {
    ariaLabel: "控制台概览",
    accounts: "账号总数",
    accountsHint: "当前已导入账号",
    running: "运行中",
    runningHint: "正在运行或请求暂停的任务",
    qrReady: "二维码就绪",
    qrReadyHint: "已有最新二维码的账号"
  },
  table: {
    title: "账号工作台",
    subtitle: (count: number) => `${count} 个账号，每 5 秒自动刷新`,
    regionLabel: "账号列表表格",
    columns: {
      account: "账号",
      product: "套餐",
      schedule: "定时",
      status: "状态",
      latestQr: "最新二维码",
      actions: "操作"
    },
    empty: "还没有导入账号",
    browserPending: "浏览器指纹待同步",
    selectProduct: "选择套餐",
    unchecked: "未检查",
    noRecentEvent: "暂无最近事件",
    run: "启动",
    pause: "暂停",
    probe: "测活",
    bizId: "bizId",
    previewConcurrency: "Preview 并发",
    scheduleState: "定时",
    scheduleEnabled: "已启用",
    scheduleDisabled: "未启用",
    syncFingerprint: "同步并换指纹",
    delete: "删除",
    deleteConfirm: "确定删除这个账号及其本地缓存吗？",
    modes: {
      newPurchase: "新购",
      upgrade: "升级"
    }
  },
  importModal: {
    title: "导入账号",
    label: "账号备注",
    labelPlaceholder: "例如 year-max-upgrade",
    invitationCode: "邀请码",
    token: "bigmodel_token_production",
    tokenPlaceholder: "粘贴 token 值",
    cancel: "取消",
    submit: "确认导入并同步"
  },
  contextModal: {
    title: "账号上下文",
    label: "备注",
    customer: "客户编号",
    name: "客户名称",
    status: "账号状态",
    schedule: "定时任务",
    browser: "浏览器指纹",
    disabled: "未启用",
    unchecked: "未检查"
  },
  schedule: {
    enableLabel: "启用定时任务",
    timeLabel: "定时启动时间"
  },
  qr: {
    alt: "最新支付二维码",
    empty: "暂无二维码"
  },
  feedback: {
    dashboardRefreshed: "列表已刷新",
    dashboardRefreshFailed: "列表刷新失败",
    operationFailed: "操作失败",
    accountImported: "账号已导入并同步",
    preferencesSaved: "偏好设置已保存",
    accountSynced: "账号已同步并更换指纹",
    accountDeleted: "账号已删除",
    paymentStarted: "支付流程已启动",
    probeStarted: "测活任务已启动",
    pauseRequested: "已请求暂停",
    openQr: "打开二维码",
    qrGenerated: (label: string, bizId: string) =>
      `账号 ${label} 已生成支付二维码${bizId ? `，bizId: ${bizId}` : ""}`,
    qrGeneratedBatch: (count: number, labels: string) => `${count} 个账号已生成支付二维码：${labels}`
  }
} as const;
