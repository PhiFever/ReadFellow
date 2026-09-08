-- 在 DataGrip 中连接 ReadFellow 数据库后执行。自由 SQL 会查询全量数据。
SET @collection = 'sample';
SET @person = '向山';
SET @run = (SELECT MAX(id) FROM runs WHERE collection = @collection AND kind = 'graph');

-- 可把 @run 改成下表中的历史 id。不同 kind 各自选择最新版本。
SELECT id AS run_id, kind, source_version_id, llm_model, prompt_version,
       created_at, updated_at, processed_count, selected_count, settings
FROM runs WHERE collection = @collection ORDER BY id DESC;

-- 某人的一跳关系（任一端命中）；每行是一条带证据的关系记录。
SELECT r.subject, r.relation, r.object, r.chapter, r.evidence,
       c.text AS chunk_text, c.source_path, c.line_start, c.line_end
FROM relations r
JOIN runs v ON v.id = r.run_id
JOIN chunks c ON c.source_version_id = r.source_version_id AND c.id = r.chunk_id
WHERE v.collection = @collection AND v.id = @run
  AND (r.subject_entity = @person OR r.object_entity = @person
       OR r.subject = @person OR r.object = @person)
ORDER BY r.chunk_index, r.line_start;

-- 有向二跳：人物 A -> B -> C；两条边必须属于同一个运行版本。
SELECT a.subject_entity AS person_a, a.relation AS relation_ab,
       a.object_entity AS person_b, b.relation AS relation_bc,
       b.object_entity AS person_c, a.evidence AS evidence_ab, b.evidence AS evidence_bc,
       c.source_path, c.line_start, c.line_end, c.text AS chunk_bc
FROM relations a
JOIN relations b ON b.run_id = a.run_id AND b.subject_position = a.object_position
JOIN runs v ON v.id = a.run_id
JOIN chunks c ON c.source_version_id = b.source_version_id AND c.id = b.chunk_id
WHERE v.collection = @collection AND v.id = @run AND a.subject_entity = @person
ORDER BY a.chunk_index, b.chunk_index;

-- 按章节、关系类型统计。直接聚合关系表，避免联接提及/别名后重复计数。
SELECT r.chapter, r.relation, COUNT(*) AS evidence_records,
       COUNT(DISTINCT r.subject_entity, r.object_entity) AS entity_pairs
FROM relations r JOIN runs v ON v.id = r.run_id
WHERE v.collection = @collection AND v.id = @run
GROUP BY r.chapter, r.relation ORDER BY MIN(r.chunk_index), evidence_records DESC;

-- 按别名找实体，再查看同版本中的提及与原文。
SELECT e.name, alias.value AS alias, m.chapter, c.source_path, c.line_start, c.line_end, c.text
FROM entities e
JOIN runs v ON v.id = e.run_id
JOIN entity_values alias ON alias.run_id = e.run_id AND alias.entity_position = e.position
JOIN entity_mentions m ON m.run_id = e.run_id AND m.entity_position = e.position
JOIN chunks c ON c.source_version_id = m.source_version_id AND c.id = m.chunk_id
WHERE v.collection = @collection AND v.id = @run AND alias.kind = 'aliases' AND alias.value = @person;

-- 章节分析有独立的运行版本。人物/事件可各自联查章节，不相互交叉联接。
SET @analysis_run = (SELECT MAX(id) FROM runs WHERE collection = @collection AND kind = 'analysis');
SELECT ch.chapter_index, ch.chapter_title, ch.summary,
       p.name, p.role_in_chapter, p.evidence, c.source_path, c.line_start, c.line_end, c.text
FROM chapters ch
JOIN runs v ON v.id = ch.run_id
LEFT JOIN characters p ON p.run_id = ch.run_id AND p.chapter_position = ch.position
LEFT JOIN chunks c ON c.source_version_id = p.source_version_id AND c.id = p.chunk_id
WHERE v.collection = @collection AND v.id = @analysis_run ORDER BY ch.chapter_index, p.sort_order;

-- 跨版本比较关系规模和锚定失败计数；NULL 表示旧产物没有记录该成因。
SELECT v.id AS run_id, v.llm_model, v.prompt_version, v.processed_count,
       v.unanchored_count, COUNT(r.position) AS relation_records
FROM runs v LEFT JOIN relations r ON r.run_id = v.id
WHERE v.collection = @collection AND v.kind = 'graph'
GROUP BY v.id ORDER BY v.id;
