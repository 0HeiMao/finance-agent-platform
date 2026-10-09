/**
 * 管理页格式化函数的单元测试。
 *
 * 用 Node 自带的测试运行器跑（`node --test tests/`），不引入 vitest / jsdom：
 * 被测函数都是纯函数，不需要 DOM。时间断言一律用「不带时区的本地时间字符串」，
 * 这样在任何时区的机器上结果都一样。
 */

import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  JOB_STATUS_LABELS,
  MAX_DEPARTMENTS,
  attemptsText,
  departmentsText,
  describeResult,
  durationText,
  formatTime,
  ownerText,
  parseDepartments,
  periodsText,
  resultText,
  retryWaitText,
  statusLabel
} from '../src/utils/format.js'

test('statusLabel 用中文标签，未知状态原样返回', () => {
  assert.equal(statusLabel('succeeded'), '已完成')
  assert.equal(statusLabel('queued'), '排队中')
  assert.equal(statusLabel('running'), '运行中')
  assert.equal(statusLabel('failed'), '失败')
  // 后端将来加了新状态也不该显示成 undefined
  assert.equal(statusLabel('paused'), 'paused')
  assert.deepEqual(Object.keys(JOB_STATUS_LABELS).sort(), ['failed', 'queued', 'running', 'succeeded'])
})

test('formatTime 补零到秒，空值与坏值都显示破折号', () => {
  assert.equal(formatTime('2026-10-08T16:23:17'), '2026-10-08 16:23:17')
  assert.equal(formatTime('2026-01-02T03:04:05'), '2026-01-02 03:04:05')
  assert.equal(formatTime(null), '—')
  assert.equal(formatTime(''), '—')
  assert.equal(formatTime('不是时间'), '—')
})

test('durationText 按开始到结束算秒数，缺字段不编造', () => {
  assert.equal(
    durationText({ started_at: '2026-10-08T16:00:00', finished_at: '2026-10-08T16:00:00.400' }),
    '0.4 秒'
  )
  assert.equal(
    durationText({ started_at: '2026-10-08T16:00:00', finished_at: '2026-10-08T16:00:51' }),
    '51.0 秒'
  )
  assert.equal(durationText({}), '—')
  assert.equal(durationText(null), '—')
})

test('resultText 只对终态给结论', () => {
  const done = {
    status: 'succeeded',
    result: { deleted_rows: 8909, recalculated: ['2026-04'], cleared: ['2026-05', '2026-06'] }
  }
  assert.equal(resultText(done), '删除 8909 行 · 重算 1 期 · 清空 2 期')
  // 没有重算/清空时不显示多余的分段
  assert.equal(
    resultText({ status: 'succeeded', result: { deleted_rows: 1, recalculated: [], cleared: [] } }),
    '删除 1 行'
  )
  assert.equal(resultText({ status: 'failed', error: '进程重启，任务未完成' }), '进程重启，任务未完成')
  assert.equal(resultText({ status: 'failed' }), '失败原因未记录')
  assert.equal(resultText({ status: 'running' }), '—')
})

test('describeResult 把期间原文列出来，便于离开页面后回看', () => {
  const text = describeResult({
    id: 20,
    result: { deleted_rows: 1, recalculated: ['2026-10'], cleared: [] }
  })
  assert.equal(text, '后台任务 #20 已完成：删除 1 行明细；重算期间：2026-10。')
})

test('attemptsText 说清楚「第几次 / 共几次」，null 与 0 都不编造进度', () => {
  assert.equal(attemptsText({ attempts: 2, max_attempts: 3 }), '第 2/3 次')
  assert.equal(attemptsText({ attempts: 1, max_attempts: 1 }), '第 1/1 次')
  // 一次都还没跑（刚提交）与字段缺失都不该显示「第 0/0 次」
  assert.equal(attemptsText({ attempts: 0, max_attempts: 3 }), '未开始')
  assert.equal(attemptsText({ attempts: null, max_attempts: null }), '未开始')
  assert.equal(attemptsText(null), '—')
  // 老任务（⑩ 之前落库的行）只有 attempts、没有 max_attempts：后端默认 3，界面也要跟后端一致
  assert.equal(attemptsText({ attempts: 2 }), '第 2/3 次')
})

test('retryWaitText 只在「排队等退避」时给时间，别的状态不假装在等', () => {
  assert.equal(
    retryWaitText({ status: 'queued', next_attempt_at: '2026-10-08T16:23:25' }),
    '约 2026-10-08 16:23:25 重试'
  )
  assert.equal(retryWaitText({ status: 'queued', next_attempt_at: null }), '—')
  assert.equal(retryWaitText({ status: 'running', next_attempt_at: '2026-10-08T16:23:25' }), '—')
  assert.equal(retryWaitText({ status: 'failed', next_attempt_at: null }), '—')
  assert.equal(retryWaitText(null), '—')
})

test('ownerText 只在运行中且记了进程时给「谁在跑」，其它状态不留上一个早已结束的进程名', () => {
  assert.equal(
    ownerText({ status: 'running', worker_id: 'finance-host-4242-ab12cd' }),
    '运行进程：finance-host-4242-ab12cd'
  )
  // 跑完/判失败/放回队列：后端会清空 worker_id，界面这里也要挡住「有值但状态不对」的老数据
  assert.equal(ownerText({ status: 'succeeded', worker_id: 'finance-host-4242-ab12cd' }), '')
  assert.equal(ownerText({ status: 'running', worker_id: null }), '')
  assert.equal(ownerText({ status: 'running', worker_id: '' }), '')
  assert.equal(ownerText({ status: 'queued' }), '')
  assert.equal(ownerText(null), '')
})

test('重跑时上次已经删干净的结果（already_reverted）不当成失败，也不说成「删除 0 行」', () => {
  const rereun = {
    id: 31,
    status: 'succeeded',
    result: { deleted_rows: 0, recalculated: [], cleared: [], already_reverted: true }
  }
  assert.equal(resultText(rereun), '该批次此前已被撤销（本次 0 行）')
  assert.equal(describeResult(rereun), '后台任务 #31 已完成：该批次此前已经被撤销过，本次没有行可删。')
})

test('periodsText 把连续期间压成区间，避免把操作列挤出视口', () => {
  const continuous = { periods: ['2026-04', '2026-05', '2026-06', '2026-07'] }
  assert.equal(periodsText(continuous), '2026-04 → 2026-07（4 期）')
  // 跨年连续也要认出来
  assert.equal(periodsText({ periods: ['2025-11', '2025-12', '2026-01'] }), '2025-11 → 2026-01（3 期）')
  assert.equal(periodsText({ periods: ['2026-10'] }), '2026-10')
  assert.equal(periodsText({ periods: [] }), '—')
  assert.equal(periodsText({}), '—')
})

test('periodsText 对不连续期间只列前三个并标总期数', () => {
  assert.equal(
    periodsText({ periods: ['2026-01', '2026-04', '2026-07', '2026-10'] }),
    '2026-01、2026-04、2026-07 等 4 期'
  )
  // 只有两个且不连续：直接列出，不加"等 N 期"
  assert.equal(periodsText({ periods: ['2026-01', '2026-03'] }), '2026-01、2026-03')
})

test('parseDepartments 支持顿号/逗号/分号/空格分隔，去空去重保序', () => {
  assert.deepEqual(parseDepartments('018、097'), ['018', '097'])
  assert.deepEqual(parseDepartments('018,097'), ['018', '097'])
  assert.deepEqual(parseDepartments('018，097；122 127'), ['018', '097', '122', '127'])
  assert.deepEqual(parseDepartments('  018 、  、097  '), ['018', '097'])
  // 重复项必须去掉：后端拿 len(scope) 当每月期望行数，重复会让它误判数据不完整
  assert.deepEqual(parseDepartments('018、018、097'), ['018', '097'])
  assert.deepEqual(parseDepartments(''), [])
  assert.deepEqual(parseDepartments(null), [])
  assert.deepEqual(parseDepartments(['018', '', ' 097 ']), ['018', '097'])
})

test('parseDepartments 截断到后端上限，不会提交后才报 422', () => {
  const many = Array.from({ length: MAX_DEPARTMENTS + 5 }, (_, index) => `D${index}`)
  const parsed = parseDepartments(many.join('、'))
  assert.equal(parsed.length, MAX_DEPARTMENTS)
  assert.equal(parsed[0], 'D0')
  assert.equal(parsed[MAX_DEPARTMENTS - 1], `D${MAX_DEPARTMENTS - 1}`)
})

test('departmentsText 用顿号回填输入框', () => {
  assert.equal(departmentsText(['018', '097']), '018、097')
  assert.equal(departmentsText(['018']), '018')
  assert.equal(departmentsText([]), '')
  assert.equal(departmentsText('部门A,部门B'), '部门A、部门B')
})
