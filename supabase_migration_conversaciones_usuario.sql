-- ══════════════════════════════════════════════════════════════
-- MIGRACIÓN: conversaciones.usuario_id
-- Ejecutar en Supabase SQL Editor (una sola vez)
-- No destructiva: columna nullable, no se tocan filas existentes.
--
-- Motivo: hoy `conversaciones` se identifica solo por cliente_id
-- (restaurante). Si dos usuarios distintos (ej. dueño y gerente) tienen
-- acceso al mismo restaurante y escriben dentro de la misma ventana de
-- 2 horas, comparten la misma fila de conversaciones y su historial se
-- mezcla. Esta columna permite distinguir conversaciones por
-- (cliente_id, usuario_id).
-- ══════════════════════════════════════════════════════════════

-- 1. Agregar columna (nullable: las filas históricas quedan con NULL,
--    no se backfillean retroactivamente)
ALTER TABLE conversaciones
    ADD COLUMN IF NOT EXISTS usuario_id UUID REFERENCES usuarios(id);

-- 2. Índice para la consulta de "conversación abierta" (get_or_create_conversacion)
CREATE INDEX IF NOT EXISTS idx_conversaciones_cliente_usuario_fecha_fin
    ON conversaciones (cliente_id, usuario_id, fecha_fin);

-- 3. Verificación: debe mostrar la columna nueva, nullable, todas las
--    filas existentes con usuario_id = NULL
SELECT id, cliente_id, usuario_id, fecha_inicio, fecha_fin
FROM conversaciones
ORDER BY fecha_inicio DESC
LIMIT 10;
