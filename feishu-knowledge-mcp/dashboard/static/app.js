/**
 * Knowledge MCP Dashboard - 前端逻辑
 *
 * 基于 Vue 3 + ECharts，通过 REST API 获取数据并渲染。
 */

const { createApp, ref, nextTick, onMounted } = Vue;

createApp({
    setup() {
        // ==================== 状态 ====================
        const stats = ref({
            total_skills: 0,
            today_saved: 0,
            week_saved: 0,
            total_searches: 0,
            total_search_attempts: 0,
            failed_searches: 0,
            exception_counts: {
                pending_index: 0,
                pending_reindex: 0,
                pending_delete: 0,
                failed: 0,
                deleted: 0,
                active_exceptions: 0,
                total_exceptions: 0,
            },
            category_distribution: {},
            project_distribution: {},
            sync_status_distribution: {},
        });
        const automationOverview = ref({
            total_sessions: 0,
            pending_review_items: 0,
            approved_review_items: 0,
            rejected_review_items: 0,
            total_auto_saved: 0,
            total_review_queued: 0,
            total_discarded: 0,
            retrieval_failed_sessions: 0,
            extraction_failed_sessions: 0,
            save_failed_sessions: 0,
            governance: {
                review_create_new: 0,
                review_merge_existing: 0,
                review_reuse_existing: 0,
                pending_with_related_skill: 0,
                approved_merge_existing: 0,
                approved_reuse_existing: 0,
                approved_create_new: 0,
            },
        });
        const governanceOverview = ref({
            review_create_new: 0,
            review_merge_existing: 0,
            review_reuse_existing: 0,
            pending_with_related_skill: 0,
            approved_merge_existing: 0,
            approved_reuse_existing: 0,
            approved_create_new: 0,
        });
        const remoteRuntime = ref({
            service: 'feishu-knowledge-mcp',
            mcp: {},
            dashboard: {},
            remote_service: {},
        });
        const hotSkills = ref([]);
        const activeTab = ref('logs');
        const exceptionRecords = ref([]);
        const registryRecords = ref([]);
        const automationSessions = ref([]);
        const reviewItems = ref([]);
        const automationMessage = ref('');
        const busyReviewIds = ref([]);

        const exceptionPagination = ref({
            total: 0,
            page: 1,
            page_size: 10,
            total_pages: 0,
        });
        const registryPagination = ref({
            total: 0,
            page: 1,
            page_size: 10,
            total_pages: 0,
        });
        const automationSessionPagination = ref({
            total: 0,
            page: 1,
            page_size: 5,
            total_pages: 0,
        });
        const reviewPagination = ref({
            total: 0,
            page: 1,
            page_size: 10,
            total_pages: 0,
        });

        const exceptionFilter = ref({
            include_deleted: true,
        });
        const registryFilter = ref({
            status: '',
            category: '',
            project: '',
            deleted: '',
        });
        const automationFilter = ref({
            status: 'pending',
            confidence: '',
            project: '',
            session_id: '',
        });
        const registryFilterOptions = ref({
            categories: [],
            projects: [],
            sync_statuses: [],
        });

        const logs = ref([]);
        const pagination = ref({
            total: 0,
            page: 1,
            page_size: 20,
            total_pages: 0,
        });

        const filter = ref({
            operation: '',
            category: '',
            date_from: '',
            date_to: '',
        });

        const categories = ref([
            '架构方案', '产品迭代', '优化沉淀', '避坑记录',
            '最佳实践', '工具使用', '业务知识',
        ]);

        const trendChart = ref(null);
        const categoryChart = ref(null);
        const syncStatusChart = ref(null);

        // ==================== 基础方法 ====================

        async function getJSON(url) {
            const res = await fetch(url);
            return await res.json();
        }

        async function postJSON(url, body = {}) {
            const res = await fetch(url, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(body),
            });
            return await res.json();
        }

        function setAutomationMessage(message) {
            automationMessage.value = message || '';
            if (!automationMessage.value) return;
            window.setTimeout(() => {
                if (automationMessage.value === message) {
                    automationMessage.value = '';
                }
            }, 4000);
        }

        function isReviewBusy(reviewId) {
            return busyReviewIds.value.includes(reviewId);
        }

        function markReviewBusy(reviewId, busy) {
            if (!reviewId) return;
            const next = busyReviewIds.value.filter(item => item !== reviewId);
            if (busy) next.push(reviewId);
            busyReviewIds.value = next;
        }

        function visiblePendingReviewIds() {
            return reviewItems.value
                .filter(item => item.status === 'pending')
                .map(item => item.review_id)
                .filter(Boolean);
        }

        // ==================== 数据加载 ====================

        async function loadStats() {
            try {
                const data = await getJSON('/api/stats/overview');
                if (!data.error) {
                    stats.value = data;
                }
            } catch (e) {
                console.error('加载统计数据失败:', e);
            }
        }

        async function loadAutomationOverview() {
            try {
                const data = await getJSON('/api/automation/overview');
                if (!data.error) {
                    automationOverview.value = {
                        ...automationOverview.value,
                        ...data,
                    };
                    governanceOverview.value = {
                        ...governanceOverview.value,
                        ...(data.governance || {}),
                    };
                }
            } catch (e) {
                console.error('加载自动化总览失败:', e);
            }
        }

        async function loadGovernanceOverview() {
            try {
                const data = await getJSON('/api/governance/overview');
                if (!data.error) {
                    governanceOverview.value = {
                        ...governanceOverview.value,
                        ...data,
                    };
                }
            } catch (e) {
                console.error('加载治理总览失败:', e);
            }
        }

        async function loadRemoteRuntime() {
            try {
                const data = await getJSON('/api/runtime/remote-service');
                if (!data.error) {
                    remoteRuntime.value = {
                        ...remoteRuntime.value,
                        ...data,
                    };
                }
            } catch (e) {
                console.error('加载远程服务运行态失败:', e);
            }
        }

        async function loadAutomationSessions() {
            try {
                const params = new URLSearchParams();
                params.set('page', automationSessionPagination.value.page);
                params.set('page_size', automationSessionPagination.value.page_size);

                const data = await getJSON(`/api/automation/sessions?${params}`);
                if (!data.error) {
                    automationSessions.value = data.sessions || [];
                    automationSessionPagination.value = {
                        total: data.total,
                        page: data.page,
                        page_size: data.page_size,
                        total_pages: data.total_pages,
                    };
                }
            } catch (e) {
                console.error('加载自动化会话失败:', e);
            }
        }

        async function loadAutomationReviews() {
            try {
                const params = new URLSearchParams();
                params.set('page', reviewPagination.value.page);
                params.set('page_size', reviewPagination.value.page_size);
                if (automationFilter.value.status) params.set('status', automationFilter.value.status);
                if (automationFilter.value.confidence) params.set('confidence', automationFilter.value.confidence);
                if (automationFilter.value.project) params.set('project', automationFilter.value.project);
                if (automationFilter.value.session_id) params.set('session_id', automationFilter.value.session_id);

                const data = await getJSON(`/api/automation/reviews?${params}`);
                if (!data.error) {
                    reviewItems.value = data.items || [];
                    reviewPagination.value = {
                        total: data.total,
                        page: data.page,
                        page_size: data.page_size,
                        total_pages: data.total_pages,
                    };
                }
            } catch (e) {
                console.error('加载审核队列失败:', e);
            }
        }

        async function loadHotSkills() {
            try {
                const data = await getJSON('/api/stats/hot-skills?top_k=8&days=30');
                if (!data.error) {
                    hotSkills.value = data.hot_skills || [];
                }
            } catch (e) {
                console.error('加载热门知识失败:', e);
            }
        }

        async function loadLogs() {
            try {
                const params = new URLSearchParams();
                params.set('page', pagination.value.page);
                params.set('page_size', pagination.value.page_size);

                if (filter.value.operation) params.set('operation', filter.value.operation);
                if (filter.value.category) params.set('category', filter.value.category);
                if (filter.value.date_from) params.set('date_from', filter.value.date_from);
                if (filter.value.date_to) params.set('date_to', filter.value.date_to);

                const data = await getJSON(`/api/logs/list?${params}`);

                if (!data.error) {
                    logs.value = data.logs || [];
                    pagination.value = {
                        total: data.total,
                        page: data.page,
                        page_size: data.page_size,
                        total_pages: data.total_pages,
                    };
                }
            } catch (e) {
                console.error('加载操作记录失败:', e);
            }
        }

        async function loadExceptions() {
            try {
                const params = new URLSearchParams();
                params.set('page', exceptionPagination.value.page);
                params.set('page_size', exceptionPagination.value.page_size);
                params.set('include_deleted', exceptionFilter.value.include_deleted ? 'true' : 'false');

                const data = await getJSON(`/api/registry/exceptions?${params}`);

                if (!data.error) {
                    exceptionRecords.value = data.records || [];
                    exceptionPagination.value = {
                        total: data.total,
                        page: data.page,
                        page_size: data.page_size,
                        total_pages: data.total_pages,
                    };
                }
            } catch (e) {
                console.error('加载异常知识失败:', e);
            }
        }

        async function loadRegistryRecords() {
            try {
                const params = new URLSearchParams();
                params.set('page', registryPagination.value.page);
                params.set('page_size', registryPagination.value.page_size);

                if (registryFilter.value.status) params.set('status', registryFilter.value.status);
                if (registryFilter.value.category) params.set('category', registryFilter.value.category);
                if (registryFilter.value.project === '__EMPTY__') {
                    params.set('project_is_empty', 'true');
                } else if (registryFilter.value.project) {
                    params.set('project', registryFilter.value.project);
                }
                if (registryFilter.value.deleted !== '') {
                    params.set('deleted', registryFilter.value.deleted);
                }

                const data = await getJSON(`/api/registry/records?${params}`);

                if (!data.error) {
                    registryRecords.value = data.records || [];
                    registryPagination.value = {
                        total: data.total,
                        page: data.page,
                        page_size: data.page_size,
                        total_pages: data.total_pages,
                    };
                    registryFilterOptions.value = {
                        categories: data.filter_options?.categories || [],
                        projects: data.filter_options?.projects || [],
                        sync_statuses: data.filter_options?.sync_statuses || [],
                    };
                }
            } catch (e) {
                console.error('加载注册表知识失败:', e);
            }
        }

        async function loadTrend() {
            try {
                const data = await getJSON('/api/stats/trend?days=30');

                if (!data.error && data.trend) {
                    await nextTick();
                    renderTrendChart(data.trend);
                }
            } catch (e) {
                console.error('加载趋势数据失败:', e);
            }
        }

        async function refreshAutomationData() {
            await Promise.all([
                loadAutomationOverview(),
                loadGovernanceOverview(),
                loadAutomationSessions(),
                loadAutomationReviews(),
            ]);
        }

        // ==================== 图表渲染 ====================

        function renderTrendChart(trend) {
            if (!trendChart.value) return;

            const chart = echarts.init(trendChart.value);
            const dates = trend.map(t => t.date.slice(5));
            const saves = trend.map(t => t.saves);
            const searches = trend.map(t => t.searches);

            chart.setOption({
                tooltip: {
                    trigger: 'axis',
                },
                legend: {
                    data: ['沉淀', '检索'],
                    bottom: 0,
                },
                grid: {
                    top: 10,
                    right: 20,
                    bottom: 40,
                    left: 40,
                },
                xAxis: {
                    type: 'category',
                    data: dates,
                    axisLabel: { fontSize: 10 },
                },
                yAxis: {
                    type: 'value',
                    minInterval: 1,
                },
                series: [
                    {
                        name: '沉淀',
                        type: 'line',
                        data: saves,
                        smooth: true,
                        areaStyle: { opacity: 0.1 },
                        itemStyle: { color: '#4CAF50' },
                    },
                    {
                        name: '检索',
                        type: 'line',
                        data: searches,
                        smooth: true,
                        areaStyle: { opacity: 0.1 },
                        itemStyle: { color: '#FF9800' },
                    },
                ],
            });

            window.addEventListener('resize', () => chart.resize());
        }

        function renderCategoryChart() {
            if (!categoryChart.value) return;

            const dist = stats.value.category_distribution;
            const chart = echarts.init(categoryChart.value);
            if (!dist || Object.keys(dist).length === 0) {
                chart.setOption({
                    title: {
                        text: '暂无数据',
                        left: 'center',
                        top: 'center',
                        textStyle: { color: '#ccc', fontSize: 16 },
                    },
                });
                return;
            }

            const pieData = Object.entries(dist).map(([name, value]) => ({ name, value }));

            chart.setOption({
                tooltip: {
                    trigger: 'item',
                    formatter: '{b}: {c} ({d}%)',
                },
                series: [
                    {
                        type: 'pie',
                        radius: ['40%', '70%'],
                        avoidLabelOverlap: false,
                        label: {
                            show: true,
                            formatter: '{b}\n{c}',
                        },
                        data: pieData,
                    },
                ],
            });

            window.addEventListener('resize', () => chart.resize());
        }

        function renderSyncStatusChart() {
            if (!syncStatusChart.value) return;

            const dist = stats.value.sync_status_distribution || {};
            const chart = echarts.init(syncStatusChart.value);
            const entries = Object.entries(dist);

            if (entries.length === 0) {
                chart.setOption({
                    title: {
                        text: '暂无数据',
                        left: 'center',
                        top: 'center',
                        textStyle: { color: '#ccc', fontSize: 16 },
                    },
                });
                return;
            }

            chart.setOption({
                tooltip: {
                    trigger: 'item',
                    formatter: '{b}: {c}',
                },
                xAxis: {
                    type: 'category',
                    data: entries.map(([name]) => name),
                    axisLabel: {
                        interval: 0,
                        rotate: 20,
                        fontSize: 10,
                    },
                },
                yAxis: {
                    type: 'value',
                    minInterval: 1,
                },
                grid: {
                    top: 10,
                    right: 10,
                    bottom: 50,
                    left: 40,
                },
                series: [
                    {
                        type: 'bar',
                        data: entries.map(([, value]) => value),
                        itemStyle: {
                            color: '#6366F1',
                            borderRadius: [6, 6, 0, 0],
                        },
                    },
                ],
            });

            window.addEventListener('resize', () => chart.resize());
        }

        // ==================== 展示辅助 ====================

        function getOpIcon(operation) {
            const icons = {
                save: '📥',
                search: '🔍',
                update: '✏️',
                delete: '🗑️',
                extract: '🧠',
                automation: '🤖',
            };
            return icons[operation] || '📄';
        }

        function getSyncStatusClass(status) {
            const mapping = {
                INDEXED: 'bg-green-50 text-green-700',
                PENDING_INDEX: 'bg-yellow-50 text-yellow-700',
                PENDING_REINDEX: 'bg-yellow-50 text-yellow-700',
                PENDING_DELETE: 'bg-orange-50 text-orange-700',
                FAILED: 'bg-red-50 text-red-700',
                DELETED: 'bg-gray-100 text-gray-600',
            };
            return mapping[status] || 'bg-slate-100 text-slate-600';
        }

        function getAutomationStatusClass(status) {
            const mapping = {
                pending: 'bg-yellow-50 text-yellow-700',
                running: 'bg-blue-50 text-blue-700',
                success: 'bg-green-50 text-green-700',
                approved: 'bg-green-50 text-green-700',
                failed: 'bg-red-50 text-red-700',
                rejected: 'bg-red-50 text-red-700',
                skipped: 'bg-slate-100 text-slate-600',
                noop: 'bg-slate-100 text-slate-600',
            };
            return mapping[status] || 'bg-slate-100 text-slate-600';
        }

        function getConfidenceClass(confidence) {
            const mapping = {
                high: 'bg-green-50 text-green-700',
                medium: 'bg-yellow-50 text-yellow-700',
                low: 'bg-slate-100 text-slate-600',
            };
            return mapping[confidence] || 'bg-slate-100 text-slate-600';
        }

        function getExceptionTitle(record) {
            if (record.deleted) return '已删除知识';
            if (record.sync_status === 'FAILED') return '同步失败';
            if (record.sync_status === 'PENDING_REINDEX') return '待重建知识';
            if (record.sync_status === 'PENDING_DELETE') return '待删除补偿';
            if (record.sync_status === 'PENDING_INDEX') return '待索引知识';
            return '异常知识';
        }

        function formatTime(timestamp) {
            if (!timestamp) return '';
            const d = new Date(timestamp);
            const now = new Date();
            const diff = now - d;

            if (diff < 60 * 1000) return '刚刚';
            if (diff < 60 * 60 * 1000) return `${Math.floor(diff / 60000)}分钟前`;
            if (diff < 24 * 60 * 60 * 1000) return `${Math.floor(diff / 3600000)}小时前`;

            return d.toLocaleDateString('zh-CN', {
                month: '2-digit',
                day: '2-digit',
                hour: '2-digit',
                minute: '2-digit',
            });
        }

        function formatAbsoluteTime(timestamp) {
            if (!timestamp) return '暂无命中';
            return new Date(timestamp).toLocaleString('zh-CN', {
                year: 'numeric',
                month: '2-digit',
                day: '2-digit',
                hour: '2-digit',
                minute: '2-digit',
            });
        }

        function formatList(items) {
            if (!items || !items.length) return '—';
            return items.join('、');
        }

        // ==================== 交互动作 ====================

        async function approveReviewItem(reviewId) {
            if (!reviewId || isReviewBusy(reviewId)) return;
            markReviewBusy(reviewId, true);
            try {
                const data = await postJSON(`/api/automation/reviews/${reviewId}/approve`);
                if (data.error) {
                    setAutomationMessage(`审批失败：${data.error}`);
                    return;
                }
                setAutomationMessage(data.message || '✅ 审核项已通过。');
                await Promise.all([loadStats(), refreshAutomationData(), loadHotSkills()]);
            } catch (e) {
                console.error('审批审核项失败:', e);
                setAutomationMessage('审批审核项失败，请稍后重试。');
            } finally {
                markReviewBusy(reviewId, false);
            }
        }

        async function rejectReviewItem(reviewId) {
            if (!reviewId || isReviewBusy(reviewId)) return;
            const reason = window.prompt('请输入驳回原因（可选）', '') || '';
            markReviewBusy(reviewId, true);
            try {
                const data = await postJSON(`/api/automation/reviews/${reviewId}/reject`, { reason });
                if (data.error) {
                    setAutomationMessage(`驳回失败：${data.error}`);
                    return;
                }
                setAutomationMessage(data.message || '🗑️ 审核项已驳回。');
                await refreshAutomationData();
            } catch (e) {
                console.error('驳回审核项失败:', e);
                setAutomationMessage('驳回审核项失败，请稍后重试。');
            } finally {
                markReviewBusy(reviewId, false);
            }
        }

        async function batchApproveVisibleReviews() {
            const reviewIds = visiblePendingReviewIds();
            if (!reviewIds.length) {
                setAutomationMessage('当前页没有待审批审核项。');
                return;
            }
            try {
                const data = await postJSON('/api/automation/reviews/batch', {
                    action: 'approve',
                    review_ids: reviewIds,
                    reason: '',
                });
                if (data.error) {
                    setAutomationMessage(`批量审批失败：${data.error}`);
                    return;
                }
                setAutomationMessage(`✅ 批量审批完成，成功 ${data.success_count || 0} 条。`);
                await Promise.all([loadStats(), refreshAutomationData(), loadHotSkills()]);
            } catch (e) {
                console.error('批量审批失败:', e);
                setAutomationMessage('批量审批失败，请稍后重试。');
            }
        }

        async function batchRejectVisibleReviews() {
            const reviewIds = visiblePendingReviewIds();
            if (!reviewIds.length) {
                setAutomationMessage('当前页没有待驳回审核项。');
                return;
            }
            const reason = window.prompt('请输入批量驳回原因（可选）', '') || '';
            try {
                const data = await postJSON('/api/automation/reviews/batch', {
                    action: 'reject',
                    review_ids: reviewIds,
                    reason,
                });
                if (data.error) {
                    setAutomationMessage(`批量驳回失败：${data.error}`);
                    return;
                }
                setAutomationMessage(`🗑️ 批量驳回完成，成功 ${data.success_count || 0} 条。`);
                await refreshAutomationData();
            } catch (e) {
                console.error('批量驳回失败:', e);
                setAutomationMessage('批量驳回失败，请稍后重试。');
            }
        }

        // ==================== 分页与筛选 ====================

        function changePage(newPage) {
            if (newPage < 1 || newPage > pagination.value.total_pages) return;
            pagination.value.page = newPage;
            loadLogs();
        }

        function changeExceptionPage(newPage) {
            if (newPage < 1 || newPage > exceptionPagination.value.total_pages) return;
            exceptionPagination.value.page = newPage;
            loadExceptions();
        }

        function changeRegistryPage(newPage) {
            if (newPage < 1 || newPage > registryPagination.value.total_pages) return;
            registryPagination.value.page = newPage;
            loadRegistryRecords();
        }

        function changeAutomationSessionPage(newPage) {
            if (newPage < 1 || newPage > automationSessionPagination.value.total_pages) return;
            automationSessionPagination.value.page = newPage;
            loadAutomationSessions();
        }

        function changeReviewPage(newPage) {
            if (newPage < 1 || newPage > reviewPagination.value.total_pages) return;
            reviewPagination.value.page = newPage;
            loadAutomationReviews();
        }

        function refreshCurrentTab() {
            if (activeTab.value === 'exceptions') {
                return loadExceptions();
            }
            if (activeTab.value === 'registry') {
                return loadRegistryRecords();
            }
            if (activeTab.value === 'governance') {
                return Promise.all([loadGovernanceOverview(), loadRemoteRuntime(), loadAutomationReviews()]);
            }
            if (activeTab.value === 'automation') {
                return refreshAutomationData();
            }
            return loadLogs();
        }

        function switchTab(tab) {
            activeTab.value = tab;
            if (tab === 'exceptions') {
                exceptionPagination.value.page = 1;
                loadExceptions();
            } else if (tab === 'registry') {
                registryPagination.value.page = 1;
                loadRegistryRecords();
            } else if (tab === 'governance') {
                reviewPagination.value.page = 1;
                Promise.all([loadGovernanceOverview(), loadRemoteRuntime(), loadAutomationReviews()]);
            } else if (tab === 'automation') {
                automationSessionPagination.value.page = 1;
                reviewPagination.value.page = 1;
                refreshAutomationData();
            } else {
                pagination.value.page = 1;
                loadLogs();
            }
        }

        function resetFilter() {
            filter.value = { operation: '', category: '', date_from: '', date_to: '' };
            pagination.value.page = 1;
            loadLogs();
        }

        function resetRegistryFilter() {
            registryFilter.value = {
                status: '',
                category: '',
                project: '',
                deleted: '',
            };
            registryPagination.value.page = 1;
            loadRegistryRecords();
        }

        function resetAutomationFilter() {
            automationFilter.value = {
                status: 'pending',
                confidence: '',
                project: '',
                session_id: '',
            };
            reviewPagination.value.page = 1;
            loadAutomationReviews();
        }

        // ==================== 生命周期 ====================

        onMounted(async () => {
            await Promise.all([
                loadStats(),
                loadAutomationOverview(),
                loadGovernanceOverview(),
                loadRemoteRuntime(),
                loadHotSkills(),
                loadLogs(),
                loadTrend(),
            ]);
            await nextTick();
            renderCategoryChart();
            renderSyncStatusChart();

            setInterval(async () => {
                await Promise.all([
                    loadStats(),
                    loadAutomationOverview(),
                    loadGovernanceOverview(),
                    loadRemoteRuntime(),
                    loadHotSkills(),
                    refreshCurrentTab(),
                ]);
                await nextTick();
                renderCategoryChart();
                renderSyncStatusChart();
            }, 30000);
        });

        return {
            stats,
            automationOverview,
            governanceOverview,
            remoteRuntime,
            hotSkills,
            activeTab,
            exceptionRecords,
            registryRecords,
            automationSessions,
            reviewItems,
            automationMessage,
            exceptionPagination,
            registryPagination,
            automationSessionPagination,
            reviewPagination,
            exceptionFilter,
            registryFilter,
            automationFilter,
            registryFilterOptions,
            logs,
            pagination,
            filter,
            categories,
            trendChart,
            categoryChart,
            syncStatusChart,
            loadLogs,
            loadExceptions,
            loadRegistryRecords,
            loadAutomationReviews,
            refreshAutomationData,
            loadGovernanceOverview,
            loadRemoteRuntime,
            switchTab,
            getOpIcon,
            getSyncStatusClass,
            getAutomationStatusClass,
            getConfidenceClass,
            getExceptionTitle,
            formatTime,
            formatAbsoluteTime,
            formatList,
            isReviewBusy,
            approveReviewItem,
            rejectReviewItem,
            batchApproveVisibleReviews,
            batchRejectVisibleReviews,
            changePage,
            changeExceptionPage,
            changeRegistryPage,
            changeAutomationSessionPage,
            changeReviewPage,
            resetFilter,
            resetRegistryFilter,
            resetAutomationFilter,
        };
    },
}).mount('#app');