// Add error line highlight style for CodeMirror
const style = document.createElement('style');
style.innerHTML = `.cm-error-line { background: #ffdddd !important; }`;
document.head.appendChild(style);
/* ── DB Testing Tool – Client-Side JavaScript ──────────────────────── */

// ── Cross-tab global state service ─────────────────────────────────
// Provides a unified way for any tab to persist/restore state with
// broadcast-channel sync so concurrent tabs stay in sync.
const AppState = (() => {
    const _channel = typeof BroadcastChannel !== 'undefined'
        ? new BroadcastChannel('db-testing-tool-state')
        : null;
    const _listeners = {};

    if (_channel) {
        _channel.onmessage = (evt) => {
            const { key } = evt.data || {};
            if (key && _listeners[key]) {
                _listeners[key].forEach(fn => {
                    try { fn(AppState.get(key)); } catch (_) {}
                });
            }
        };
    }

    return {
        /** Save state by key. Notifies other tabs via BroadcastChannel. */
        set(key, value) {
            try {
                localStorage.setItem(key, JSON.stringify(value));
                if (_channel) _channel.postMessage({ key });
            } catch (_) {}
        },

        /** Read state by key. Returns fallback if not found or corrupt. */
        get(key, fallback = null) {
            try {
                const raw = localStorage.getItem(key);
                if (raw === null) return fallback;
                const parsed = JSON.parse(raw);
                return parsed !== null && parsed !== undefined ? parsed : fallback;
            } catch (_) {
                return fallback;
            }
        },

        /** Remove a state key. */
        remove(key) {
            localStorage.removeItem(key);
        },

        /** Subscribe to changes from other tabs for a given key. Returns unsubscribe fn. */
        onChange(key, callback) {
            if (!_listeners[key]) _listeners[key] = [];
            _listeners[key].push(callback);
            return () => {
                _listeners[key] = (_listeners[key] || []).filter(fn => fn !== callback);
            };
        },
    };
})();

// ── Toast notifications ────────────────────────────────────────────
function showToast(message, type = 'info') {
    let container = document.querySelector('.app-toast-container');
    if (!container) {
        // Backward compatibility: reuse any legacy container if present.
        container = document.querySelector('.toast-container');
        if (container) {
            container.className = 'app-toast-container';
        }
    }
    if (!container) {
        container = document.createElement('div');
        container.className = 'app-toast-container';
        document.body.appendChild(container);
    }
    const toneMap = { danger: 'error', failed: 'error', ok: 'success' };
    const normalizedType = toneMap[type] || type;
    const toast = document.createElement('div');
    toast.className = `app-toast app-toast-${normalizedType}`;
    toast.textContent = message;
    toast.title = 'Click to copy';
    toast.style.cursor = 'pointer';
    toast.addEventListener('click', async () => {
        try {
            if (navigator.clipboard?.writeText) {
                await navigator.clipboard.writeText(String(message || ''));
            }
        } catch (_err) {
            // Clipboard write failures should not affect notifications.
        }
    });
    container.appendChild(toast);
    setTimeout(() => {
        if (toast && toast.parentNode) toast.remove();
    }, 7000);
}

// ── API helpers ────────────────────────────────────────────────────
async function api(method, url, body = null, options = null) {
    const opts = { method, headers: { 'Content-Type': 'application/json' } };
    if (body) opts.body = JSON.stringify(body);
    if (options && options.signal) opts.signal = options.signal;
        const res = await fetch(url, opts).catch(err => {
            throw new Error(`Network error: ${err.message}`);
        });
    if (!res.ok) {
        const text = await res.text();
        let msg = text;
        try {
            const parsed = JSON.parse(text);
            if (parsed && typeof parsed === 'object') {
                const d = parsed.detail;
                if (typeof d === 'string') msg = d;
                else if (d && typeof d === 'object') msg = d.error || d.message || JSON.stringify(d);
                else if (parsed.error) msg = String(parsed.error);
            }
        } catch (_) {}
        throw new Error(`${res.status}: ${msg}`);
    }
    return res.json();
}

const _nativeConfirm = window.confirm.bind(window);
function isAutoApproveEnabled() {
    const raw = localStorage.getItem('app.autoApproveConfirmations');
    if (raw === null) {
        localStorage.setItem('app.autoApproveConfirmations', '0');
        return false;
    }
    return raw === '1';
}
window.confirm = function(message) {
    if (isAutoApproveEnabled()) return true;
    return _nativeConfirm(message);
};

const API = {
    // Datasources
    getDatasources: () => api('GET', '/api/datasources'),
    getDatasource: (id) => api('GET', `/api/datasources/${id}`),
    exportDatasourcesEnv: () => api('GET', '/api/datasources/export-env'),
    createDatasource: (d) => api('POST', '/api/datasources', d),
    updateDatasource: (id, d) => api('PUT', `/api/datasources/${id}`, d),
    testConnection: (id) => api('POST', `/api/datasources/${id}/test`),
    queryDatasource: (id, sqlOrPayload, options = null) => {
        const payload = (typeof sqlOrPayload === 'object' && sqlOrPayload !== null)
            ? sqlOrPayload
            : { sql: sqlOrPayload };
        return api('POST', `/api/datasources/${id}/query`, payload, options);
    },
    deleteDatasource: (id) => api('DELETE', `/api/datasources/${id}`),

    // Schemas
    analyzeSchema: (dsId, filterOrOptions, requestOptions = null) => {
        if (typeof filterOrOptions === 'object' && filterOrOptions !== null) {
            return api('POST', '/api/schemas/analyze', {
                datasource_id: dsId,
                schema_filter: filterOrOptions.schema_filter || null,
                schema_filters: Array.isArray(filterOrOptions.schema_filters) ? filterOrOptions.schema_filters : null,
                save_to_kb: !!filterOrOptions.save_to_kb,
                operation_id: filterOrOptions.operation_id || null,
                background: !!filterOrOptions.background,
            }, requestOptions);
        }
        return api('POST', '/api/schemas/analyze', {
            datasource_id: dsId,
            schema_filter: filterOrOptions || null,
            background: false,
        }, requestOptions);
    },
    getDatasourceSchemas: (dsId) => api('GET', `/api/schemas/catalog/${dsId}`),
    generatePdm: (dsId, schemas = [], saveToKb = true, operationId = null, requestOptions = null, background = false) => api('POST', '/api/schemas/pdm', {
        datasource_id: dsId,
        schemas: Array.isArray(schemas) ? schemas : [],
        save_to_kb: !!saveToKb,
        operation_id: operationId || null,
        background: !!background,
    }, requestOptions),
    saveSchemaKb: (dsId, schemas = [], operationId = null, requestOptions = null, background = false) => api('POST', '/api/schemas/kb/save', {
        datasource_id: dsId,
        schemas: Array.isArray(schemas) ? schemas : [],
        save_to_kb: true,
        operation_id: operationId || null,
        background: !!background,
    }, requestOptions),
    stopSchemaOperation: (operationId) => api('POST', `/api/schemas/stop/${encodeURIComponent(operationId)}`, {}),
    getSchemaOperation: (operationId) => api('GET', `/api/schemas/operation/${encodeURIComponent(operationId)}`),
    getSchemaTree: (dsId) => api('GET', `/api/schemas/tree/${dsId}`),
    compareSchemas: (d) => api('POST', '/api/schemas/compare', d),
    getSavedPdm: (dsId) => api('GET', `/api/schemas/saved-pdm/${dsId}`),
    listPdmHistory: () => api('GET', '/api/schemas/pdm-history'),
    scanOwnerSchemas: (dsId, operationId = null, requestOptions = null) => api('POST', '/api/schemas/scan-owner', {
        datasource_id: dsId,
        operation_id: operationId || null,
    }, requestOptions),


    // Mappings
    getMappings: () => api('GET', '/api/mappings'),
    getMapping: (id) => api('GET', `/api/mappings/${id}`),
    createMapping: (d) => api('POST', '/api/mappings', d),
    updateMapping: (id, d) => api('PUT', `/api/mappings/${id}`, d),
    deleteMapping: (id) => api('DELETE', `/api/mappings/${id}`),
    bulkDeleteMappings: (ids) => api('POST', '/api/mappings/bulk-delete', { ids }),

    // Tests
    getTests: () => api('GET', '/api/tests'),
    getTest: (id) => api('GET', `/api/tests/${id}`),
    createTest: (d) => api('POST', '/api/tests', d),
    updateTest: (id, d) => api('PUT', `/api/tests/${id}`, d),
    deleteTest: (id) => api('DELETE', `/api/tests/${id}`),
    bulkDeleteTests: (ids) => api('POST', '/api/tests/bulk-delete', { ids }),
    bulkDeleteRuns: (ids) => api('POST', '/api/tests/runs/bulk-delete', { ids }),
    generateTests: (ruleId, connectionId) => api('POST', `/api/tests/generate/${ruleId}${connectionId ? '?connection_id=' + connectionId : ''}`),
    generateAllTests: (connectionId) => api('POST', `/api/tests/generate-all${connectionId ? '?connection_id=' + connectionId : ''}`),
    previewTests: (ruleId) => api('POST', `/api/tests/preview/${ruleId}`),
    createSelectedTests: (tests) => api('POST', '/api/tests/create-selected', { tests }),
    validateTestSql: (tests, datasourceId) => api('POST', '/api/tests/validate-sql', { tests, datasource_id: datasourceId }),
    drdPreview: async (file) => {
        const form = new FormData();
        form.append('file', file);
        const res = await fetch('/api/mappings/drd-preview', { method: 'POST', body: form });
        if (!res.ok) {
            const text = await res.text();
            throw new Error(`${res.status}: ${text}`);
        }
        return res.json();
    },
    buildEmptyControlTable: (payload) => api('POST', '/api/tests/control-table/empty', payload),
    analyzeControlTable: async (payload) => {
        const form = new FormData();
        const resolvedTargetDs = payload.targetDatasourceId || payload.sourceDatasourceId || '';
        form.append('file', payload.file);
        form.append('target_schema', payload.targetSchema || '');
        form.append('target_table', payload.targetTable || '');
        form.append('source_datasource_id', String(payload.sourceDatasourceId || ''));
        form.append('target_datasource_id', String(resolvedTargetDs));
        form.append('control_schema', payload.controlSchema || '');
        form.append('main_grain', payload.mainGrain || '');
        form.append('manual_sql', payload.manualSql || '');
        form.append('sheet_name', payload.sheetName || '');
        if (Array.isArray(payload.selectedFields)) {
            for (const field of payload.selectedFields) {
                form.append('selected_fields', field);
            }
        }
        const res = await fetch('/api/tests/control-table/analyze', { method: 'POST', body: form });
        if (!res.ok) {
            const text = await res.text();
            let msg = text;
            try {
                const parsed = JSON.parse(text);
                const d = parsed?.detail;
                if (typeof d === 'string') msg = d;
                else if (d && typeof d === 'object') msg = d.error || d.message || JSON.stringify(d);
            } catch (_) {}
            throw new Error(`${res.status}: ${msg}`);
        }
        return res.json();
    },
    previewControlTableDrd: async (file, sheetName = '') => {
        const form = new FormData();
        form.append('file', file);
        form.append('sheet_name', sheetName || '');
        const res = await fetch('/api/tests/control-table/preview-drd', { method: 'POST', body: form });
        if (!res.ok) {
            const text = await res.text();
            throw new Error(`${res.status}: ${text}`);
        }
        return res.json();
    },
    compareControlTableSql: (analysisRows, generatedSql, manualSql = '', targetTable = '', compareMode = 'all') => api('POST', '/api/tests/control-table/compare', {
        analysis_rows: analysisRows,
        generated_sql: generatedSql,
        manual_sql: manualSql,
        target_table: targetTable,
        compare_mode: compareMode,
    }),
    compareControlTableDocs: async (payload) => {
        const form = new FormData();
        if (payload?.drd_file) form.append('drd_file', payload.drd_file);
        if (payload?.odi_file_1) form.append('odi_file_1', payload.odi_file_1);
        if (payload?.odi_file_2) form.append('odi_file_2', payload.odi_file_2);
        if (typeof payload?.manual_sql === 'string') form.append('manual_sql', payload.manual_sql);
        const res = await fetch('/api/tests/control-table/compare-docs', { method: 'POST', body: form });
        if (!res.ok) {
            const text = await res.text();
            let msg = text;
            try {
                const parsed = JSON.parse(text);
                const d = parsed?.detail;
                if (typeof d === 'string') msg = d;
                else if (d && typeof d === 'object') msg = d.error || d.message || JSON.stringify(d);
            } catch (_) {}
            throw new Error(`${res.status}: ${msg}`);
        }
        return res.json();
    },
    listControlTableTrainingRules: (targetTable) => api('GET', `/api/tests/control-table/training/rules?target_table=${encodeURIComponent(targetTable || '')}`),
    saveControlTableTrainingFeedback: (payload) => api('POST', '/api/tests/control-table/training/feedback', payload),
    listControlTableTrainingFixtures: () => api('GET', '/api/tests/control-table/training/fixtures'),
    replayControlTableTraining: (payload) => api('POST', '/api/tests/control-table/training/replay', payload),
    applyControlTableDecisions: (baseSql, decisions, targetTable = '', fileFingerprint = '', fileName = '') => api('POST', '/api/tests/control-table/apply', {
        base_sql: baseSql,
        decisions,
        target_table: targetTable,
        file_fingerprint: fileFingerprint,
        file_name: fileName,
    }),
    applyControlTableSqlVariant: (baseSql, variantSql) => api('POST', '/api/tests/control-table/apply-sql', {
        base_sql: baseSql,
        variant_sql: variantSql,
    }),
    checkControlTableInsertSql: (targetDatasourceId, sql, execute = false) => api('POST', '/api/tests/control-table/check-insert', {
        target_datasource_id: targetDatasourceId,
        sql,
        execute,
    }),
    clearControlTableFileState: (targetTable, fileFingerprint = '') => {
        const params = new URLSearchParams({ target_table: targetTable });
        if (fileFingerprint) params.append('file_fingerprint', fileFingerprint);
        return api('DELETE', `/api/tests/control-table/file-state?${params.toString()}`);
    },
    saveControlTableInsertState: (payload) => api('POST', '/api/tests/control-table/save-insert-state', payload || {}),
    // Phase 7.19.8 (2026-06-02): backend route is /control-table/suite
    // (no -save prefix). Pre-fix URL /control-table/save-suite returned
    // 404 -> "Save failed: 404" the operator reported on 2026-06-01.
    saveControlTableSuite: (suiteName, tests) => api('POST', '/api/tests/control-table/suite', {
        suite_name: suiteName,
        tests,
    }),
    exportTestsToTfsCsv: async (testIds, areaPath = '', assignedTo = '', state = 'Design') => {
        const res = await fetch('/api/tests/export-tfs-csv', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ test_ids: testIds, area_path: areaPath, assigned_to: assignedTo, state }),
        });
        if (!res.ok) { const t = await res.text(); throw new Error(`${res.status}: ${t}`); }
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        const cd = res.headers.get('content-disposition') || '';
        const m = cd.match(/filename="?([^"]+)"?/);
        a.download = m ? m[1] : 'tfs_tests.csv';
        a.click();
        URL.revokeObjectURL(url);
    },
    runTest: (id) => api('POST', `/api/tests/run/${id}`),
    runBatch: (ids) => api('POST', '/api/tests/run-batch', { test_ids: ids || null }),
    startBatch: (ids) => api('POST', '/api/tests/run-batch/start', { test_ids: ids || null }),
    getBatchStatus: (batchId) => api('GET', `/api/tests/run-batch/status/${encodeURIComponent(batchId)}`),
    stopBatch: (batchId) => api('POST', `/api/tests/run-batch/stop/${encodeURIComponent(batchId)}`, {}),
    clearRuns: (batchId) => api('POST', `/api/tests/runs/clear${batchId ? '?batch_id=' + encodeURIComponent(batchId) : ''}`, {}),
    clearAllRunStatuses: () => api('POST', '/api/tests/runs/clear-all-statuses', {}),
    getRuns: (batchId) => api('GET', `/api/tests/runs${batchId ? '?batch_id=' + batchId : ''}`),
    getTestFolders: () => api('GET', '/api/tests/folders'),
    createTestFolder: (name) => api('POST', '/api/tests/folders', { name }),
    deleteTestFolder: (folderId) => api('DELETE', `/api/tests/folders/${folderId}`),
    bulkDeleteTestFolders: (folderIds) => api('POST', '/api/tests/folders/bulk-delete', { folder_ids: folderIds }),
    moveTestsToFolder: (testIds, folderId) => api('POST', '/api/tests/folders/move', { test_ids: testIds, folder_id: folderId }),
    setFolderDatasource: (folderId, payload) => api('POST', `/api/tests/folders/${folderId}/datasource`, payload),
    getRun: (id) => api('GET', `/api/tests/runs/${id}`),
    getDashboardStats: () => api('GET', '/api/tests/dashboard-stats'),
    logTrainingEvent: (payload) => api('POST', '/api/tests/training-events', payload || {}),
    getTrainingAutomationStatus: () => api('GET', '/api/tests/training-automation/status'),
    startTrainingAutomation: (payload) => api('POST', '/api/tests/training-automation/start', payload || {}),
    stopTrainingAutomation: () => api('POST', '/api/tests/training-automation/stop', {}),
    runTrainingAutomationOnce: (payload) => api('POST', '/api/tests/training-automation/run-once', payload || {}),
    runTrainingPipeline: (payload) => api('POST', '/api/tests/training-pipeline/run', payload || {}),
    listTrainingPipelineRules: (targetTable) => api('GET', `/api/tests/training-pipeline/rules?target_table=${encodeURIComponent(targetTable || '')}`),
    saveTrainingPack: async (payload) => {
        const form = new FormData();
        form.append('target_table', payload.target_table || '');
        form.append('source_tables', payload.source_tables || '');
        form.append('notes', payload.notes || '');
        form.append('reference_sql', payload.reference_sql || '');
        form.append('validation_sql', payload.validation_sql || '');
        form.append('source_datasource_id', payload.source_datasource_id || '');
        form.append('target_datasource_id', payload.target_datasource_id || '');
        for (const file of (payload.drd_files || [])) {
            form.append('drd_files', file);
        }
        const res = await fetch('/api/tests/training-packs', { method: 'POST', body: form });
        if (!res.ok) {
            const text = await res.text();
            throw new Error(`${res.status}: ${text}`);
        }
        return res.json();
    },
    deriveTrainingPackContext: async (payload) => {
        const form = new FormData();
        form.append('target_table', payload.target_table || '');
        form.append('source_tables', payload.source_tables || '');
        form.append('source_sql', payload.source_sql || '');
        form.append('expected_sql', payload.expected_sql || '');
        for (const file of (payload.drd_files || [])) {
            form.append('drd_files', file);
        }
        const res = await fetch('/api/tests/training-packs/derive-context', { method: 'POST', body: form });
        if (!res.ok) {
            const text = await res.text();
            throw new Error(`${res.status}: ${text}`);
        }
        return res.json();
    },

    // AI
    extractRules: (sql, agentIds = [], taskHint = '') => api('POST', '/api/ai/extract-rules', { sql_text: sql, agent_ids: agentIds, task_hint: taskHint }),
    suggestTests: (rule, schema, agentIds = [], taskHint = '') => api('POST', '/api/ai/suggest-tests', { mapping_rule: rule, schema_info: schema, agent_ids: agentIds, task_hint: taskHint }),
    triageFailures: (failures) => api('POST', '/api/ai/triage', { failures }),
    analyzeSql: (sql, agentIds = [], taskHint = '') => api('POST', '/api/ai/analyze-sql', { sql_text: sql, agent_ids: agentIds, task_hint: taskHint }),
    compareMappingSql: (payload) => api('POST', '/api/ai/compare-mapping-sql', payload),
    aiChat: (messages, context, provider, agentIds = [], taskHint = '', attachments = []) => api('POST', '/api/ai/chat', { messages, context: context || '', provider: provider || '', agent_ids: agentIds, task_hint: taskHint, attachments }),
    startTrainingReproduceAsync: (messages, context, provider, agentIds = [], taskHint = '', attachments = []) => api('POST', '/api/ai/training-reproduce-async', { messages, context: context || '', provider: provider || '', agent_ids: agentIds, task_hint: taskHint, attachments }),
    getTrainingReproduceJobStatus: (jobId) => api('GET', `/api/ai/training-reproduce-async/${encodeURIComponent(jobId)}`),
    copilotStatus: () => api('GET', '/api/ai/copilot/status'),
    copilotStartDevice: () => api('POST', '/api/ai/copilot/device/start', {}),
    copilotPollDevice: (deviceCode) => api('POST', '/api/ai/copilot/device/poll', { device_code: deviceCode }),
    copilotLogout: () => api('POST', '/api/ai/copilot/logout', {}),

    // Local Agents
    getAgents: () => api('GET', '/api/agents'),
    createAgent: (d) => api('POST', '/api/agents', d),
    updateAgent: (id, d) => api('PUT', `/api/agents/${id}`, d),
    deleteAgent: (id) => api('DELETE', `/api/agents/${id}`),
    seedDefaultAgents: () => api('POST', '/api/agents/seed-defaults', {}),

    // TFS
    getWorkItems: () => api('GET', '/api/tfs/workitems'),
    getWorkItem: (id) => api('GET', `/api/tfs/workitems/${id}`),
    createBug: (d) => api('POST', '/api/tfs/workitems', d),
    updateBug: (id, d) => api('PUT', `/api/tfs/workitems/${id}`, d),
    deleteBug: (id) => api('DELETE', `/api/tfs/workitems/${id}`),
    syncWorkItem: (id) => api('POST', `/api/tfs/workitems/${id}/sync`),
    autoBugs: (batchId) => api('POST', '/api/tfs/auto-bugs', { batch_id: batchId }),
    getTfsProjects: () => api('GET', '/api/tfs/projects'),
    getTfsConfig: () => api('GET', '/api/tfs/config'),
    runTfsQuery: (project, query) => api('POST', '/api/tfs/query', { project, query }),
    getSavedQueries: (project, folder, depth = null) => {
        let url = `/api/tfs/saved-queries/${encodeURIComponent(project)}?folder=${encodeURIComponent(folder || '')}`;
        if (depth !== null && depth !== undefined) url += `&depth=${encodeURIComponent(depth)}`;
        return api('GET', url);
    },
    runSavedQuery: (project, queryId) => api('GET', `/api/tfs/saved-queries/${encodeURIComponent(project)}/run/${encodeURIComponent(queryId)}`),
    getPresetQueries: () => api('GET', '/api/tfs/preset-queries'),
    getTfsClassificationNodes: (project, structureGroup, depth = 6) => api('GET', `/api/tfs/classification-nodes/${encodeURIComponent(project)}?structure_group=${encodeURIComponent(structureGroup)}&depth=${encodeURIComponent(depth)}`),
    createTfsTestPlan: (payload) => api('POST', '/api/tfs/test-plans', payload),
    createTfsTestSuite: (payload) => api('POST', '/api/tfs/test-suites', payload),
    importLocalTestsToTfsSuite: (payload) => api('POST', '/api/tfs/test-suites/import-local-tests', payload),
    importTfsPointsToLocal: (payload) => api('POST', '/api/tfs/test-points/import-local', payload),

    // TFS Test Execution
    get: (url) => api('GET', url),
    post: (url, body) => api('POST', url, body),
    put: (url, body) => api('PUT', url, body),
    delete: (url) => api('DELETE', url),
    getTestPlans: (project) => api('GET', `/api/tfs/test-plans/${encodeURIComponent(project)}`),
    getTestSuites: (project, planId, parentSuiteId = null) => {
        let url = `/api/tfs/test-suites/${encodeURIComponent(project)}/${planId}`;
        if (parentSuiteId) url += `?parent_suite_id=${parentSuiteId}`;
        return api('GET', url);
    },
    getTestPoints: (project, planId, suiteId) => api('GET', `/api/tfs/test-points/${encodeURIComponent(project)}/${planId}/${suiteId}`),
    createTestRun: (payload) => api('POST', '/api/tfs/test-runs', payload),
    getTestRun: (runId) => api('GET', `/api/tfs/test-runs/${runId}`),
    executeTfsTestPoint: (runId, testPointId, payload) => api('POST', `/api/tfs/test-runs/${runId}/tests/${testPointId}/execute`, payload),
    updateTestResult: (runId, testPointId, payload) => api('PUT', `/api/tfs/test-runs/${runId}/tests/${testPointId}/result`, payload),
    completeTestRun: (runId) => api('POST', `/api/tfs/test-runs/${runId}/complete`),
    syncRegressionCatalog: (payload) => api('POST', '/api/regression-lab/sync', payload),
    getRegressionCatalog: (project, opts = {}) => {
        const params = new URLSearchParams();
        if (opts.group) params.append('group', opts.group);
        if (opts.status) params.append('status', opts.status);
        if (opts.searchText) params.append('search_text', opts.searchText);
        if (opts.areaPath) params.append('area_path', opts.areaPath);
        if (opts.iterationPath) params.append('iteration_path', opts.iterationPath);
        if (opts.planName) params.append('plan_name', opts.planName);
        if (opts.suiteName) params.append('suite_name', opts.suiteName);
        if (opts.owner) params.append('owner', opts.owner);
        if (opts.title) params.append('title', opts.title);
        if (opts.tags) params.append('tags', opts.tags);
        if (opts.minChangedDate) params.append('min_changed_date', opts.minChangedDate);
        if (opts.includeExcluded) params.append('include_excluded', String(!!opts.includeExcluded));
        const query = params.toString();
        return api('GET', `/api/regression-lab/catalog/${encodeURIComponent(project)}${query ? '?' + query : ''}`);
    },
    getRegressionSettings: (project) => api('GET', `/api/regression-lab/settings/${encodeURIComponent(project)}`),
    patchRegressionSettings: (project, payload) => api('PATCH', `/api/regression-lab/settings/${encodeURIComponent(project)}`, payload),
    excludeRegressionByFilters: (payload) => api('POST', '/api/regression-lab/exclusions/by-filters', payload),
    getRegressionGroups: (project) => api('GET', `/api/regression-lab/groups/${encodeURIComponent(project)}`),
    getRegressionReport: (project) => api('GET', `/api/regression-lab/report/${encodeURIComponent(project)}`),
    runRegressionSearchAgent: (payload) => api('POST', '/api/regression-lab/search-agent', payload),
    runRegressionValidationAgent: (payload) => api('POST', '/api/regression-lab/validate-agent', payload),
    promoteRegressionItems: (payload) => api('POST', '/api/regression-lab/promote', payload),
    getRegressionFilters: (project, filterText = '') => {
        const params = filterText ? `?filter_text=${encodeURIComponent(filterText)}` : '';
        return api('GET', `/api/regression-lab/filters/${encodeURIComponent(project)}${params}`);
    },

    // DRD AI summary
    drdAiSummary: async (file, selectedFields, targetSchema, targetTable, sourceTable, sqlText, mainGrain, aiMode, sourceDsId, targetDsId, singleDbTesting = true, crossDbOptional = true) => {
        const params = new URLSearchParams({
            selected_fields: selectedFields || '',
            target_schema: targetSchema || '',
            target_table: targetTable || '',
            source_table: sourceTable || '',
            sql_text: sqlText || '',
            main_grain: mainGrain || '',
            ai_mode: aiMode || 'ghc_kb',
            source_datasource_id: sourceDsId || '1',
            target_datasource_id: targetDsId || '1',
            single_db_testing: String(!!singleDbTesting),
            cross_db_optional: String(!!crossDbOptional),
        });
        const form = new FormData();
        form.append('file', file);
        const res = await fetch(`/api/mappings/drd-ai-summary?${params.toString()}`, {
            method: 'POST',
            body: form,
        });
        if (!res.ok) {
            const text = await res.text();
            throw new Error(`${res.status}: ${text}`);
        }
        return res.json();
    },

    // AI mapping file analysis
    aiExtractRulesFromFile: async (file, targetTable, sqlText, agentIds, taskHint) => {
        const form = new FormData();
        form.append('file', file);
        const params = new URLSearchParams({
            target_table: targetTable || '',
            sql_text: sqlText || '',
            agent_ids: (agentIds || []).join(','),
            task_hint: taskHint || '',
        });
        const res = await fetch(`/api/ai/extract-rules-from-mapping-file?${params.toString()}`, {
            method: 'POST',
            body: form,
        });
        if (!res.ok) {
            const text = await res.text();
            throw new Error(`${res.status}: ${text}`);
        }
        return res.json();
    },

    aiGenerateTestsFromFile: async (file, targetTable, generationMode, samplePerCategory, agentIds, taskHint) => {
        const form = new FormData();
        form.append('file', file);
        const params = new URLSearchParams({
            target_table: targetTable || '',
            generation_mode: generationMode || 'sample',
            sample_per_category: samplePerCategory || '2',
            agent_ids: (agentIds || []).join(','),
            task_hint: taskHint || '',
        });
        const res = await fetch(`/api/ai/generate-tests-from-mapping-file?${params.toString()}`, {
            method: 'POST',
            body: form,
        });
        if (!res.ok) {
            const text = await res.text();
            throw new Error(`${res.status}: ${text}`);
        }
        return res.json();
    },

    // ── Credential Profiles ──────────────────────────────────────────────────
    listCredentials: () => api('GET', '/api/credentials'),
    createCredential: (body) => api('POST', '/api/credentials', body),
    getCredential: (id) => api('GET', `/api/credentials/${id}`),
    injectCredential: (id) => api('GET', `/api/credentials/${id}/inject`),
    updateCredential: (id, body) => api('PUT', `/api/credentials/${id}`, body),
    deleteCredential: (id) => api('DELETE', `/api/credentials/${id}`),

    // External desktop tools
    getExternalToolsStatus: () => api('GET', '/api/external-tools/status'),
    detectExternalTools: () => api('GET', '/api/external-tools/detect'),
    getExternalToolsConfig: () => api('GET', '/api/external-tools/config'),
    getExternalToolDefaultStreamUrl: (tool) => api('GET', `/api/external-tools/stream/default-url?tool=${encodeURIComponent(tool || '')}`),
    saveExternalToolsConfig: (odiPath = '', sqldeveloperPath = '', odiStreamUrl = '', sqldeveloperStreamUrl = '') => api('POST', '/api/external-tools/config/save', {
        odi_path: odiPath,
        sqldeveloper_path: sqldeveloperPath,
        odi_stream_url: odiStreamUrl,
        sqldeveloper_stream_url: sqldeveloperStreamUrl,
    }),
    launchExternalTool: (tool, path = '', args = '') => api('POST', '/api/external-tools/launch', { tool, path, args }),
    stopExternalTool: (tool) => api('POST', '/api/external-tools/stop', { tool }),

    // ODI runtime / monitoring
    getOdiConfigFiles: (rootPath = '', maxFiles = 300) => api('GET', `/api/odi/config-files?root_path=${encodeURIComponent(rootPath || '')}&max_files=${encodeURIComponent(maxFiles)}`),
    getOdiLogins: (rootPath = '') => api('GET', `/api/odi/logins?root_path=${encodeURIComponent(rootPath || '')}`),
    connectOdiRepository: (payload) => api('POST', '/api/odi/repository/connect', payload || {}),
    getOdiRepositoryStatus: (ownerToken = '') => api('GET', `/api/odi/repository/status?owner_token=${encodeURIComponent(ownerToken || '')}`),
    analyzeOdiFiles: async (files, query = {}) => {
        const form = new FormData();
        for (const file of (files || [])) form.append('files', file);
        const params = new URLSearchParams({
            owner_token: query.owner_token || '',
            login_name: query.login_name || '',
        });
        const res = await fetch(`/api/odi/analyze-files?${params.toString()}`, { method: 'POST', body: form });
        if (!res.ok) {
            const text = await res.text();
            throw new Error(`${res.status}: ${text}`);
        }
        return res.json();
    },
    getOdiContexts: (ownerToken = '', loginName = '', rootPath = '') => api('GET', `/api/odi/contexts?owner_token=${encodeURIComponent(ownerToken || '')}&login_name=${encodeURIComponent(loginName || '')}&root_path=${encodeURIComponent(rootPath || '')}`),
    getOdiAgents: (ownerToken = '', loginName = '') => api('GET', `/api/odi/agents?owner_token=${encodeURIComponent(ownerToken || '')}&login_name=${encodeURIComponent(loginName || '')}`),
    searchOdiPackages: (q = '', limit = 120, query = {}) => api('GET', `/api/odi/packages/search?q=${encodeURIComponent(q || '')}&limit=${encodeURIComponent(limit)}&owner_token=${encodeURIComponent(query.owner_token || '')}&login_name=${encodeURIComponent(query.login_name || '')}`),
    runOdiPackage: (payload) => api('POST', '/api/odi/run', payload || {}),
    listOdiSessions: (query = {}) => {
        const params = new URLSearchParams({
            owner_token: query.owner_token || '',
            only_mine: String(query.only_mine !== false),
            name_contains: query.name_contains || '',
            status: query.status || '',
            tracked_only: String(!!query.tracked_only),
            tracked_session_id: query.tracked_session_id || '',
            limit: String(query.limit || 200),
        });
        return api('GET', `/api/odi/sessions?${params.toString()}`);
    },
    getOdiSession: (sessionId) => api('GET', `/api/odi/sessions/${encodeURIComponent(sessionId)}`),
    cancelOdiSession: (sessionId) => api('POST', `/api/odi/sessions/${encodeURIComponent(sessionId)}/cancel`, {}),

    // Optional live-window stream panel (remote URL only)
    checkExternalToolStream: (tool, url = '') => api('GET', `/api/external-tools/stream/check?tool=${encodeURIComponent(tool || '')}&url=${encodeURIComponent(url || '')}`),

    // Watchdog monitoring
    getWatchdogStatus: () => api('GET', '/api/system/watchdog/status'),
    runWatchdogSweep: () => api('POST', '/api/system/watchdog/sweep', {}),
};

function openExternalWindow(url) {
    if (!url) return;
    window.open(url, '_blank', 'width=1600,height=950');
}

const TFS_PATH_MEMORY_KEY = 'dbTestingTool.tfsPathMemory.v1';

function _readTfsPathMemory() {
    try {
        return JSON.parse(localStorage.getItem(TFS_PATH_MEMORY_KEY) || '{}');
    } catch (_) {
        return {};
    }
}

function _writeTfsPathMemory(payload) {
    localStorage.setItem(TFS_PATH_MEMORY_KEY, JSON.stringify(payload || {}));
}

function getRememberedTfsPaths(project, context) {
    const memory = _readTfsPathMemory();
    return memory?.[project || '']?.[context || ''] || {};
}

function rememberTfsPaths(project, context, values) {
    const projectKey = String(project || '').trim();
    const contextKey = String(context || '').trim();
    if (!projectKey || !contextKey) return;
    const memory = _readTfsPathMemory();
    memory[projectKey] = memory[projectKey] || {};
    memory[projectKey][contextKey] = {
        area_path: String(values?.area_path || '').trim(),
        iteration_path: String(values?.iteration_path || '').trim(),
        saved_at: new Date().toISOString(),
    };
    _writeTfsPathMemory(memory);
}

window.getRememberedTfsPaths = getRememberedTfsPaths;
window.rememberTfsPaths = rememberTfsPaths;


// ── Modal helper ───────────────────────────────────────────────────
function openModal(id) {
    const overlay = document.getElementById(id);
    if (!overlay) return;
    overlay.classList.add('active');
    // Initialize editors that may have been injected into the modal
    setTimeout(initAllSqlEditors, 50);
}
function closeModal(id) {
    const overlay = document.getElementById(id);
    if (!overlay) return;
    const modal = overlay.querySelector('.modal');
    if (modal && !modal.classList.contains('ct-modal-shell')) {
        setModalFullscreen(modal, false);
    }
    overlay.classList.remove('modal-overlay-fullscreen');
    overlay.classList.remove('active');
}

function getPrimaryModal(overlay) {
    return overlay?.querySelector('.modal') || null;
}

function getModalHeader(modal) {
    if (!modal) return null;
    return modal.querySelector(':scope > .modal-header') || modal.querySelector('.modal-header');
}

function ensureModalHeaderActions(header) {
    if (!header) return null;
    
    // Try to find existing actions container
    let actions = header.querySelector(':scope > .modal-header-actions');
    if (!actions) {
        actions = header.querySelector(':scope > .ct-modal-header-actions');
    }
    if (actions) return actions;

    // Create new actions container
    actions = document.createElement('div');
    actions.className = 'modal-header-actions';
    
    // Move close button to actions if it exists at header root level
    const closeButton = header.querySelector(':scope > .modal-close');
    if (closeButton) {
        actions.appendChild(closeButton);
    }
    
    header.appendChild(actions);
    return actions;
}

function syncModalFullscreenButton(modal) {
    const button = modal?.querySelector('.modal-fullscreen-toggle');
    if (!button) return;
    const isFullscreen = modal.classList.contains('fullscreen');
    button.textContent = isFullscreen ? 'Windowed' : 'Full Screen';
    button.setAttribute('aria-pressed', isFullscreen ? 'true' : 'false');
    button.title = isFullscreen ? 'Restore window size' : 'Expand to full screen';

    const badge = modal?.querySelector('.modal-size-badge');
    if (badge) {
        const snap = _snapshotModalSize(modal);
        badge.textContent = `${isFullscreen ? 'FS' : 'WND'} ${snap.width}x${snap.height}`;
    }
}

function setModalFullscreen(modal, shouldBeFullscreen) {
    if (!modal || modal.classList.contains('ct-modal-shell')) return;
    const overlay = modal.closest('.modal-overlay');
    const isFullscreen = shouldBeFullscreen ?? !modal.classList.contains('fullscreen');

    const before = _snapshotModalSize(modal);
    if (isFullscreen) {
        const rect = modal.getBoundingClientRect();
        modal.dataset.windowedWidth = `${Math.round(rect.width)}px`;
        modal.dataset.windowedHeight = modal.style.height || '';
        modal.dataset.windowedMaxWidth = modal.style.maxWidth || '';
        modal.dataset.windowedMaxHeight = modal.style.maxHeight || '';
        modal.classList.add('fullscreen');
        if (overlay) overlay.classList.add('modal-overlay-fullscreen');
        modal.style.width = '100vw';
        modal.style.height = '100vh';
        modal.style.maxWidth = '100vw';
        modal.style.maxHeight = '100vh';
    } else {
        modal.classList.remove('fullscreen');
        if (overlay) overlay.classList.remove('modal-overlay-fullscreen');
        modal.style.width = modal.dataset.windowedWidth || modal.style.width || '900px';
        modal.style.height = modal.dataset.windowedHeight || '';
        modal.style.maxWidth = modal.dataset.windowedMaxWidth || '96vw';
        modal.style.maxHeight = modal.dataset.windowedMaxHeight || '94vh';
    }
    const after = _snapshotModalSize(modal);
    _recordModalFullscreenMeasurement(modal, before, after, isFullscreen);

    syncModalFullscreenButton(modal);
    setTimeout(initAllSqlEditors, 30);
}

function _snapshotModalSize(modal) {
    const r = modal?.getBoundingClientRect();
    return {
        width: Math.round((r?.width || 0) * 100) / 100,
        height: Math.round((r?.height || 0) * 100) / 100,
        viewportWidth: window.innerWidth,
        viewportHeight: window.innerHeight,
    };
}

function _recordModalFullscreenMeasurement(modal, before, after, isFullscreen) {
    try {
        if (!window.__modalFullscreenRegression) window.__modalFullscreenRegression = [];
        const entry = {
            modalId: modal?.id || '',
            action: isFullscreen ? 'fullscreen' : 'windowed',
            before,
            after,
            timestamp: new Date().toISOString(),
        };
        window.__modalFullscreenRegression.push(entry);
        if (window.__modalFullscreenRegression.length > 200) {
            window.__modalFullscreenRegression = window.__modalFullscreenRegression.slice(-200);
        }
        console.info('[modal-size-check]', entry);
    } catch (_err) {
        // No-op: telemetry should never block UI interactions.
    }
}

window.getModalFullscreenRegression = function() {
    return Array.isArray(window.__modalFullscreenRegression) ? window.__modalFullscreenRegression.slice() : [];
};

function prepareModalWindow(overlay) {
    const modal = getPrimaryModal(overlay);
    if (!modal) return;

    modal.classList.add('app-modal-window');
    const header = getModalHeader(modal);

    // Add fullscreen toggle button if not present
    if (!modal.classList.contains('ct-modal-shell') && header) {
        let button = header.querySelector('.modal-fullscreen-toggle');
        if (!button) {
            const actions = ensureModalHeaderActions(header);
            if (actions) {
                let badge = header.querySelector('.modal-size-badge');
                if (!badge) {
                    badge = document.createElement('span');
                    badge.className = 'badge badge-info modal-size-badge';
                    badge.style.lineHeight = '28px';
                    badge.style.fontSize = '11px';
                    actions.prepend(badge);
                }
                button = document.createElement('button');
                button.type = 'button';
                button.className = 'btn btn-outline btn-sm modal-fullscreen-toggle';
                button.title = 'Toggle fullscreen';
                button.addEventListener('click', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    setModalFullscreen(modal);
                });
                actions.prepend(button);
            }
        }
    }

    // Initialize modal window sizing on first prepare
    if (!modal.dataset.windowPrepared && !modal.classList.contains('ct-modal-shell')) {
        const rect = modal.getBoundingClientRect();
        if (!modal.style.width && rect.width > 0) modal.style.width = `${Math.round(rect.width)}px`;
        if (!modal.style.maxWidth) modal.style.maxWidth = '96vw';
        if (!modal.style.maxHeight) modal.style.maxHeight = '94vh';
        modal.dataset.windowPrepared = '1';
    }

    syncModalFullscreenButton(modal);
}

function initModalWindows() {
    document.querySelectorAll('.modal-overlay').forEach(prepareModalWindow);
}
// Intentionally do not close modals on overlay click.
// Users must close via explicit in-modal controls (for example, the X button).

const sqlEditorInstances = new WeakMap();
const SQL_EDITOR_THEME_KEY = 'ui.sql.editor.theme.v1';

function getSqlEditorThemePreference() {
    return localStorage.getItem(SQL_EDITOR_THEME_KEY) === 'light' ? 'light' : 'dark';
}

function applySqlEditorTheme(theme) {
    const normalized = theme === 'light' ? 'light' : 'dark';
    document.body.classList.remove('sql-theme-dark', 'sql-theme-light');
    document.body.classList.add(normalized === 'light' ? 'sql-theme-light' : 'sql-theme-dark');
    return normalized;
}

function setSqlEditorTheme(theme) {
    const normalized = applySqlEditorTheme(theme);
    localStorage.setItem(SQL_EDITOR_THEME_KEY, normalized);
    initAllSqlEditors();
    return normalized;
}

window.getSqlEditorThemePreference = getSqlEditorThemePreference;
window.applySqlEditorTheme = applySqlEditorTheme;
window.setSqlEditorTheme = setSqlEditorTheme;

// Initialize CodeMirror editors for SQL textareas (idempotent)
function initAllSqlEditors() {
    if (typeof CodeMirror === 'undefined') return;
    const uiTheme = getSqlEditorThemePreference();
    const cmTheme = uiTheme === 'light' ? 'eclipse' : 'material-darker';
    applySqlEditorTheme(uiTheme);
    document.querySelectorAll('textarea.sql-editor').forEach(ta => {
        try {
            let cm = sqlEditorInstances.get(ta);
            if (!cm && ta.nextSibling && ta.nextSibling.CodeMirror) {
                cm = ta.nextSibling.CodeMirror;
                sqlEditorInstances.set(ta, cm);
            }

            if (!cm && !ta.dataset.codemirror) {
                const _hasFold = typeof CodeMirror.prototype.foldCode === 'function';
                const _hasHint = !!(CodeMirror.hint && CodeMirror.hint.sql);
                cm = CodeMirror.fromTextArea(ta, {
                    mode: 'text/x-sql',
                    theme: cmTheme,
                    lineNumbers: true,
                    matchBrackets: true,
                    indentWithTabs: true,
                    tabSize: 4,
                    autoCloseBrackets: true,
                    foldGutter: _hasFold,
                    gutters: _hasFold ? ['CodeMirror-linenumbers', 'CodeMirror-foldgutter'] : ['CodeMirror-linenumbers'],
                    hintOptions: { completeSingle: false },
                });
                ta.dataset.codemirror = '1';
                sqlEditorInstances.set(ta, cm);
                cm.on('change', () => { ta.value = cm.getValue(); });
            }

            if (cm) {
                cm.setOption('theme', cmTheme);
                const desiredValue = ta.value || '';
                if (cm.getValue() !== desiredValue) {
                    cm.setValue(desiredValue);
                }
                cm.refresh();
            }
        } catch (e) {
            console.error('Failed to init CodeMirror for', ta, e);
        }
    });
}

function setSqlEditorValue(textareaId, value) {
    const ta = document.getElementById(textareaId);
    if (!ta) return;
    ta.value = value || '';

    let cm = sqlEditorInstances.get(ta);
    if (!cm && ta.nextSibling && ta.nextSibling.CodeMirror) {
        cm = ta.nextSibling.CodeMirror;
        sqlEditorInstances.set(ta, cm);
    }

    if (cm) {
        if (cm.getValue() !== ta.value) {
            cm.setValue(ta.value);
        }
        cm.refresh();
    }
}

function getSqlEditorValue(textareaId) {
    const ta = document.getElementById(textareaId);
    if (!ta) return '';

    let cm = sqlEditorInstances.get(ta);
    if (!cm && ta.nextSibling && ta.nextSibling.CodeMirror) {
        cm = ta.nextSibling.CodeMirror;
        sqlEditorInstances.set(ta, cm);
    }

    return cm ? cm.getValue() : (ta.value || '');
}

// ── Tree toggle ────────────────────────────────────────────────────
document.addEventListener('click', e => {
    if (e.target.classList.contains('tree-toggle')) {
        e.target.classList.toggle('open');
        const children = e.target.nextElementSibling;
        if (children) children.classList.toggle('open');
    }
});

// ── Utility ────────────────────────────────────────────────────────
function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}

function statusBadge(status) {
    const map = {
        passed: 'success', ok: 'success',
        failed: 'danger', error: 'danger',
        running: 'info', pending: 'muted',
        untested: 'muted', New: 'info',
        Active: 'warning', Resolved: 'success', Closed: 'muted',
    };
    return `<span class="badge badge-${map[status] || 'muted'}">${escapeHtml(status)}</span>`;
}

function severityBadge(sev) {
    const map = { critical: 'danger', high: 'warning', medium: 'info', low: 'muted' };
    return `<span class="badge badge-${map[sev] || 'muted'}">${escapeHtml(sev)}</span>`;
}

// ── Global layout toggle (sidebar hide/show) ──────────────────────
function applySidebarState(collapsed) {
    if (collapsed) document.body.classList.add('sidebar-collapsed');
    else document.body.classList.remove('sidebar-collapsed');
    const btn = document.getElementById('sidebar-toggle-btn');
    if (btn) btn.textContent = collapsed ? '⮞' : '☰';
}

function initSidebarToggle() {
    const btn = document.getElementById('sidebar-toggle-btn');
    if (!btn) return;
    const collapsed = localStorage.getItem('layout.sidebarCollapsed') === '1';
    applySidebarState(collapsed);
    btn.addEventListener('click', () => {
        const next = !document.body.classList.contains('sidebar-collapsed');
        applySidebarState(next);
        localStorage.setItem('layout.sidebarCollapsed', next ? '1' : '0');
    });
}

document.addEventListener('DOMContentLoaded', initSidebarToggle);
document.addEventListener('DOMContentLoaded', initModalWindows);
document.addEventListener('DOMContentLoaded', () => {
    applySqlEditorTheme(getSqlEditorThemePreference());
});

// ── Datasource form population helper ──────────────────────────────
async function populateDatasourceSelects() {
    try {
        const sources = await API.getDatasources();
        document.querySelectorAll('select.ds-select').forEach(sel => {
            const val = sel.value;
            sel.innerHTML = '<option value="">-- Select --</option>' +
                sources.map(s => `<option value="${s.id}">${escapeHtml(s.name)} (${s.db_type})</option>`).join('');
            if (val) sel.value = val;
        });
    } catch (e) {
        console.error('Failed to load datasources', e);
    }
}

// ── Global schema/table predictions for source/target inputs ───────────────
const _globalSchemaTableHintCache = {}; // dsId -> { tables: {}, loaded: bool, loading: bool, callbacks: [] }
let _globalSchemaTableHintWired = false;

function _getGlobalHintEntry(dsId) {
    const key = String(dsId || '');
    if (!key) return null;
    if (!_globalSchemaTableHintCache[key]) {
        _globalSchemaTableHintCache[key] = { tables: {}, loaded: false, loading: false, callbacks: [] };
    }
    return _globalSchemaTableHintCache[key];
}

function _resolveDatasourceIdForInput(config) {
    const explicit = document.getElementById(config.dsSelectId || '')?.value || '';
    if (explicit) return explicit;
    const fallbackIds = Array.isArray(config.fallbackDsSelectIds) ? config.fallbackDsSelectIds : [];
    for (const id of fallbackIds) {
        const v = document.getElementById(id || '')?.value || '';
        if (v) return v;
    }
    const fallback = document.querySelector('select.ds-select')?.value || '';
    return fallback;
}

function _ownerTableKeysFromHintTables(tables) {
    return Object.keys(tables || {}).filter(k => String(k || '').includes('.')).map(k => String(k).trim());
}

function _ownerNamesFromHintTables(tables) {
    const owners = new Set();
    for (const k of _ownerTableKeysFromHintTables(tables)) {
        owners.add(k.split('.', 1)[0]);
    }
    return [...owners].sort((a, b) => String(a).localeCompare(String(b)));
}

function _tableNamesForOwner(tables, owner) {
    const ownerU = String(owner || '').toUpperCase();
    if (!ownerU) return [];
    const out = [];
    for (const key of _ownerTableKeysFromHintTables(tables)) {
        const parts = key.split('.');
        if (parts.length !== 2) continue;
        if (String(parts[0] || '').toUpperCase() === ownerU) out.push(parts[1]);
    }
    return [...new Set(out)].sort((a, b) => String(a).localeCompare(String(b)));
}

function _withCaseVariants(items) {
    const out = [];
    const seen = new Set();
    for (const item of (items || [])) {
        const v = String(item || '').trim();
        if (!v) continue;
        const variants = [v, v.toLowerCase()];
        for (const vv of variants) {
            if (!seen.has(vv)) {
                seen.add(vv);
                out.push(vv);
            }
        }
    }
    return out;
}

async function _loadGlobalSchemaTableHint(dsId) {
    const key = String(dsId || '');
    if (!key) return {};
    const entry = _getGlobalHintEntry(key);
    if (!entry) return {};
    if (entry.loaded) return entry.tables;
    if (entry.loading) {
        return new Promise(resolve => {
            entry.callbacks.push((tables) => resolve(tables || {}));
            setTimeout(() => resolve(entry.tables || {}), 8000);
        });
    }

    entry.loading = true;
    const _finalize = (tables, loaded = true) => {
        entry.tables = tables || {};
        entry.loaded = !!loaded;
        entry.loading = false;
        const cbs = entry.callbacks || [];
        entry.callbacks = [];
        for (const cb of cbs) { try { cb(entry.tables); } catch (_) {} }
    };

    try {
        // Fast path: small local hint index from KB/PDM
        const res = await fetch(`/api/schemas/hint-tables/${encodeURIComponent(key)}`);
        if (res.ok) {
            const data = await res.json();
            if (data?.loading) {
                // background index build underway, small backoff then retry once
                await new Promise(r => setTimeout(r, 1200));
                const retry = await fetch(`/api/schemas/hint-tables/${encodeURIComponent(key)}`);
                if (retry.ok) {
                    const retryData = await retry.json();
                    if (retryData?.tables && Object.keys(retryData.tables).length) {
                        _finalize(retryData.tables, true);
                        return entry.tables;
                    }
                }
            } else if (data?.tables && Object.keys(data.tables).length) {
                _finalize(data.tables, true);
                return entry.tables;
            }
        }

        // Fallback: datasource query (works for non-LH sources too)
        const q = await API.queryDatasource(parseInt(key, 10), {
            sql: 'SELECT OWNER, TABLE_NAME FROM ALL_TABLES ORDER BY 1,2 FETCH FIRST 5000 ROWS ONLY',
            row_limit: 5000,
        });
        const tables = {};
        for (const r of (q?.rows || [])) {
            const owner = String(r.OWNER || '').toUpperCase().trim();
            const table = String(r.TABLE_NAME || '').toUpperCase().trim();
            if (owner && table) tables[`${owner}.${table}`] = [];
        }
        _finalize(tables, true);
        return entry.tables;
    } catch (_) {
        _finalize({}, false);
        return {};
    }
}

function _ensureInputDatalist(input, suffix) {
    if (!input) return null;
    const dlId = `${input.id || 'schema-table-input'}-${suffix || 'pred'}-dl`;
    let dl = document.getElementById(dlId);
    if (!dl) {
        dl = document.createElement('datalist');
        dl.id = dlId;
        document.body.appendChild(dl);
    }
    input.setAttribute('list', dlId);
    input.setAttribute('autocomplete', 'off');
    return dl;
}

function _setDatalistOptions(dl, items, maxItems = 100) {
    if (!dl) return;
    const unique = [];
    const seen = new Set();
    for (const item of (items || [])) {
        const v = String(item || '').trim();
        if (!v || seen.has(v)) continue;
        seen.add(v);
        unique.push(v);
        if (unique.length >= maxItems) break;
    }
    dl.innerHTML = unique.map(v => `<option value="${escapeHtml(v)}"></option>`).join('');
}

let _schemaHintStyleInjected = false;
let _activeSchemaHintInput = null;

function _ensureSchemaHintStyle() {
    if (_schemaHintStyleInjected) return;
    _schemaHintStyleInjected = true;
    const style = document.createElement('style');
    style.textContent = `
.schema-hints {
  position: fixed;
  z-index: 12000;
  min-width: 220px;
  max-width: 520px;
  max-height: 260px;
  overflow-y: auto;
  background: #102a4d;
  border: 1px solid rgba(96,165,250,.55);
  border-radius: 6px;
  box-shadow: 0 10px 24px rgba(0,0,0,.45);
}
.schema-hints-item {
  padding: 6px 10px;
  font-family: monospace;
  font-size: 13px;
  line-height: 1.3;
  color: #22c55e;
  cursor: pointer;
  white-space: nowrap;
}
.schema-hints-item.active,
.schema-hints-item:hover {
  background: #173b6b;
  color: #e2e8f0;
}
`;
    document.head.appendChild(style);
}

function _hideSchemaHintPopup(input) {
    const popup = input?._schemaHintPopup;
    if (!popup) return;
    popup.style.display = 'none';
    input._schemaHintItems = [];
    input._schemaHintIndex = -1;
    if (_activeSchemaHintInput === input) _activeSchemaHintInput = null;
}

function _ensureSchemaHintPopup(input) {
    if (!input) return null;
    _ensureSchemaHintStyle();
    if (!input._schemaHintPopup) {
        const popup = document.createElement('div');
        popup.className = 'schema-hints';
        popup.style.display = 'none';
        document.body.appendChild(popup);
        input._schemaHintPopup = popup;
    }
    return input._schemaHintPopup;
}

function _replaceLastCsvToken(raw, replacement) {
    const src = String(raw || '');
    const tokenRaw = src.split(',').pop() || '';
    const prefix = src.slice(0, src.length - tokenRaw.length);
    return `${prefix}${replacement}`;
}

function _buildSchemaTablePopupItems(rawValue, tables) {
    const raw = String(rawValue || '');
    const tokenRaw = raw.split(',').pop() || '';
    const token = tokenRaw.trim();
    if (!token) return [];

    const keys = _ownerTableKeysFromHintTables(tables);
    const tokenU = token.toUpperCase();
    const dot = token.indexOf('.');
    const items = [];
    const seen = new Set();
    const push = (label, fullToken) => {
        const k = `${label}|||${fullToken}`;
        if (seen.has(k)) return;
        seen.add(k);
        items.push({
            label,
            value: _replaceLastCsvToken(raw, fullToken),
        });
    };

    if (dot < 0) {
        const owners = _ownerNamesFromHintTables(tables);
        for (const owner of owners) {
            const ownerU = String(owner || '').toUpperCase();
            if (!ownerU.startsWith(tokenU)) continue;
            push(owner, `${owner}.`);
            if (items.length >= 80) break;
        }
        return items;
    }

    const ownerRaw = token.slice(0, dot).trim();
    const ownerU = ownerRaw.toUpperCase();
    const part = token.slice(dot + 1).trim().toUpperCase();
    for (const key of keys) {
        const idx = key.indexOf('.');
        if (idx < 0) continue;
        const ko = key.slice(0, idx).toUpperCase();
        const kt = key.slice(idx + 1);
        if (ko !== ownerU) continue;
        if (part && !kt.toUpperCase().startsWith(part)) continue;
        push(kt, `${ownerRaw}.${kt}`);
        if (items.length >= 120) break;
    }
    return items;
}

function _positionSchemaHintPopup(input, popup) {
    if (!input || !popup) return;
    const rect = input.getBoundingClientRect();
    popup.style.left = `${Math.round(rect.left)}px`;
    popup.style.top = `${Math.round(rect.bottom + 4)}px`;
    popup.style.width = `${Math.round(rect.width)}px`;
}

function _renderSchemaHintPopup(input, items) {
    const popup = _ensureSchemaHintPopup(input);
    if (!popup) return;
    input._schemaHintItems = items || [];
    input._schemaHintIndex = (items && items.length) ? 0 : -1;
    if (!items || !items.length) {
        _hideSchemaHintPopup(input);
        return;
    }
    popup.innerHTML = items.map((it, idx) =>
        `<div class="schema-hints-item${idx === 0 ? ' active' : ''}" data-idx="${idx}">${escapeHtml(it.label)}</div>`
    ).join('');
    popup.querySelectorAll('.schema-hints-item').forEach(el => {
        el.addEventListener('mousedown', (e) => {
            e.preventDefault();
            const idx = parseInt(el.dataset.idx || '-1', 10);
            const choice = input._schemaHintItems?.[idx];
            if (!choice) return;
            input.value = choice.value;
            input.dispatchEvent(new Event('input', { bubbles: true }));
            _hideSchemaHintPopup(input);
        });
    });
    _positionSchemaHintPopup(input, popup);
    popup.style.display = 'block';
    _activeSchemaHintInput = input;
}

function _moveSchemaHintSelection(input, dir) {
    const popup = input?._schemaHintPopup;
    const items = input?._schemaHintItems || [];
    if (!popup || !items.length) return;
    const next = Math.max(0, Math.min(items.length - 1, (input._schemaHintIndex || 0) + dir));
    input._schemaHintIndex = next;
    popup.querySelectorAll('.schema-hints-item').forEach((el, idx) => {
        el.classList.toggle('active', idx === next);
        if (idx === next) el.scrollIntoView({ block: 'nearest' });
    });
}

function _acceptSchemaHintSelection(input) {
    const idx = Number.isFinite(input?._schemaHintIndex) ? input._schemaHintIndex : -1;
    const choice = input?._schemaHintItems?.[idx >= 0 ? idx : 0];
    if (!choice) return false;
    input.value = choice.value;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    _hideSchemaHintPopup(input);
    return true;
}

function _schemaTableSuggestions(value, tables) {
    const raw = String(value || '');
    const token = raw.split(',').pop().trim();
    if (!token) return [];
    const tokenU = token.toUpperCase();
    const keys = _ownerTableKeysFromHintTables(tables);
    const owners = _ownerNamesFromHintTables(tables);

    const dot = token.indexOf('.');
    if (dot < 0) {
        const ownerMatches = owners.filter(o => String(o || '').toUpperCase().startsWith(tokenU)).map(o => `${o}.`);
        const ownerPrefixMatches = keys.filter(k => String(k || '').toUpperCase().startsWith(tokenU));
        const tableNameMatches = keys.filter(k => {
            const ks = String(k || '');
            const idx = ks.indexOf('.');
            if (idx < 0) return false;
            const tableName = ks.slice(idx + 1).toUpperCase();
            return tableName.includes(tokenU);
        });
        return _withCaseVariants([...ownerMatches, ...ownerPrefixMatches, ...tableNameMatches]);
    }

    const owner = token.slice(0, dot);
    const part = token.slice(dot + 1);
    const ownerU = owner.toUpperCase();
    const partU = part.toUpperCase();
    return _withCaseVariants(
        keys.filter(k => {
            const ks = String(k || '');
            const idx = ks.indexOf('.');
            if (idx < 0) return false;
            const ko = ks.slice(0, idx).toUpperCase();
            const kt = ks.slice(idx + 1);
            return ko === ownerU && (!partU || kt.toUpperCase().includes(partU));
        })
    );
}

function _schemaOnlySuggestions(value, tables) {
    const tokenRaw = String(value || '').trim();
    const token = tokenRaw.toUpperCase();
    if (!token) return [];
    return _withCaseVariants(_ownerNamesFromHintTables(tables).filter(o => String(o || '').toUpperCase().startsWith(token)));
}

function _tableOnlySuggestions(value, tables, schemaValue) {
    const schema = String(schemaValue || '').trim();
    const schemaU = schema.toUpperCase();
    const tokenRaw = String(value || '').trim();
    const token = tokenRaw.toUpperCase();
    if (!schema || !token) return [];
    return _withCaseVariants(_tableNamesForOwner(tables, schemaU).filter(t => String(t || '').toUpperCase().startsWith(token)));
}

function _wirePredictInput(config) {
    const input = document.getElementById(config.inputId);
    if (!input || input.dataset.schemaPredBound === '1') return;
    input.dataset.schemaPredBound = '1';

    const mode = String(config.mode || 'schema-table');
    const isSchemaTableMode = mode.toLowerCase() === 'schematable';
    if (isSchemaTableMode) {
        input.removeAttribute('list');
        input.setAttribute('autocomplete', 'off');
    }

    const dl = isSchemaTableMode ? null : _ensureInputDatalist(input, mode);

    const refresh = async () => {
        const dsId = _resolveDatasourceIdForInput(config);
        if (!dsId) {
            if (isSchemaTableMode) _hideSchemaHintPopup(input);
            return;
        }
        const tables = await _loadGlobalSchemaTableHint(dsId);
        let items = [];
        if (isSchemaTableMode) {
            items = _buildSchemaTablePopupItems(input.value, tables);
            _renderSchemaHintPopup(input, items);
            return;
        }
        if (config.mode === 'schema') {
            items = _schemaOnlySuggestions(input.value, tables);
        } else if (config.mode === 'table') {
            const schemaInput = document.getElementById(config.schemaInputId || '');
            items = _tableOnlySuggestions(input.value, tables, schemaInput?.value || '');
        } else {
            items = _schemaTableSuggestions(input.value, tables);
        }
        _setDatalistOptions(dl, items, 120);
    };

    input.addEventListener('focus', refresh);
    input.addEventListener('input', refresh);
    if (isSchemaTableMode) {
        input.addEventListener('keydown', (e) => {
            if (input?._schemaHintPopup?.style.display !== 'block') return;
            if (e.key === 'ArrowDown') { e.preventDefault(); _moveSchemaHintSelection(input, 1); return; }
            if (e.key === 'ArrowUp') { e.preventDefault(); _moveSchemaHintSelection(input, -1); return; }
            if (e.key === 'Enter' || e.key === 'Tab') {
                if (_acceptSchemaHintSelection(input)) e.preventDefault();
                return;
            }
            if (e.key === 'Escape') { e.preventDefault(); _hideSchemaHintPopup(input); }
        });
        input.addEventListener('blur', () => {
            setTimeout(() => _hideSchemaHintPopup(input), 120);
        });
    }

    if (config.dsSelectId) {
        const dsSelect = document.getElementById(config.dsSelectId);
        if (dsSelect && dsSelect.dataset.schemaPredDsBound !== '1') {
            dsSelect.dataset.schemaPredDsBound = '1';
            dsSelect.addEventListener('change', refresh);
        }
    }
    if (config.schemaInputId) {
        const schemaInput = document.getElementById(config.schemaInputId);
        if (schemaInput && schemaInput.dataset.schemaPredSchemaBound !== '1') {
            schemaInput.dataset.schemaPredSchemaBound = '1';
            schemaInput.addEventListener('input', refresh);
        }
    }
}

document.addEventListener('click', (e) => {
    const input = _activeSchemaHintInput;
    if (!input) return;
    const popup = input._schemaHintPopup;
    if (!popup) return;
    if (e.target === input || popup.contains(e.target)) return;
    _hideSchemaHintPopup(input);
});

window.addEventListener('resize', () => {
    const input = _activeSchemaHintInput;
    if (!input || !input._schemaHintPopup || input._schemaHintPopup.style.display !== 'block') return;
    _positionSchemaHintPopup(input, input._schemaHintPopup);
});

function wireGlobalSchemaTablePredictions() {
    const configs = [
        // Training Studio
        { inputId: 'ts-target-table', dsSelectId: 'ts-target-ds', fallbackDsSelectIds: ['ts-source-ds'], mode: 'schemaTable' },
        { inputId: 'ts-source-tables', dsSelectId: 'ts-source-ds', mode: 'schemaTable' },
        // Mappings + Control Table modal
        { inputId: 'drd-target', dsSelectId: 'drd-tgt-ds', fallbackDsSelectIds: ['drd-src-ds'], mode: 'schemaTable' },
        { inputId: 'drd-source-table', dsSelectId: 'drd-src-ds', fallbackDsSelectIds: ['drd-tgt-ds'], mode: 'schemaTable' },
        { inputId: 'drd-ai-detected-source', dsSelectId: 'drd-src-ds', fallbackDsSelectIds: ['drd-tgt-ds'], mode: 'schemaTable' },
        { inputId: 'drd-ai-detected-target', dsSelectId: 'drd-tgt-ds', fallbackDsSelectIds: ['drd-src-ds'], mode: 'schemaTable' },
        { inputId: 'drd-ai-detected-lookup', dsSelectId: 'drd-src-ds', fallbackDsSelectIds: ['drd-tgt-ds'], mode: 'schemaTable' },
        { inputId: 'ct-target', dsSelectId: 'ct-target-ds', fallbackDsSelectIds: ['ct-source-ds'], mode: 'schemaTable' },
        { inputId: 'ct-source-table', dsSelectId: 'ct-source-ds', mode: 'schemaTable' },
        { inputId: 'ct-lookup-tables', dsSelectId: 'ct-source-ds', fallbackDsSelectIds: ['ct-target-ds'], mode: 'schemaTable' },
        // AI Assistant target
        { inputId: 'ai-mapping-target', dsSelectId: 'ct-target-ds', fallbackDsSelectIds: ['ct-source-ds'], mode: 'schemaTable' },
        // Schema Browser compare form
        { inputId: 'cmp-src-schema', dsSelectId: 'cmp-src-ds', mode: 'schema' },
        { inputId: 'cmp-src-table', dsSelectId: 'cmp-src-ds', mode: 'table', schemaInputId: 'cmp-src-schema' },
        { inputId: 'cmp-tgt-schema', dsSelectId: 'cmp-tgt-ds', mode: 'schema' },
        { inputId: 'cmp-tgt-table', dsSelectId: 'cmp-tgt-ds', mode: 'table', schemaInputId: 'cmp-tgt-schema' },
        // Rule editor modal target table
        { inputId: 'rule-ed-table', dsSelectId: 'ct-target-ds', fallbackDsSelectIds: ['ct-source-ds'], mode: 'schemaTable' },
    ];
    configs.forEach(_wirePredictInput);
}

function initGlobalSchemaTablePredictions() {
    if (_globalSchemaTableHintWired) return;
    _globalSchemaTableHintWired = true;
    wireGlobalSchemaTablePredictions();
    // Some page controls are rendered/updated after initial load.
    let runs = 0;
    const timer = setInterval(() => {
        wireGlobalSchemaTablePredictions();
        runs += 1;
        if (runs >= 8) clearInterval(timer);
    }, 750);

    // Keep wiring late-rendered modal fields (e.g., AI detected table editors).
    let observeTimer = null;
    const observer = new MutationObserver(() => {
        if (observeTimer) return;
        observeTimer = setTimeout(() => {
            observeTimer = null;
            wireGlobalSchemaTablePredictions();
        }, 120);
    });
    observer.observe(document.body, { childList: true, subtree: true });
}

document.addEventListener('DOMContentLoaded', initGlobalSchemaTablePredictions);
