-- ══════════════════════════════════════════════════════════════
-- MIGRACIÓN: usuarios.pregunta_pendiente / pregunta_pendiente_ts
-- Ejecutar en Supabase SQL Editor (una sola vez)
-- No destructiva: columnas nullable, no se tocan filas existentes.
--
-- Motivo: cuando el bot corta para preguntar "¿cuál restaurante?" (caso
-- de ambigüedad, ver MENSAJE_ELEGIR_RESTAURANTE), la pregunta original
-- del usuario se perdía y había que repetirla después de elegir el
-- restaurante. Estas columnas guardan esa pregunta original por usuario,
-- para poder reusarla apenas responda cuál restaurante quiere consultar
-- (dentro de una ventana de 10 minutos).
-- ══════════════════════════════════════════════════════════════

-- 1. Agregar columnas (nullable: usuarios sin pregunta pendiente quedan en NULL)
ALTER TABLE usuarios
    ADD COLUMN IF NOT EXISTS pregunta_pendiente TEXT;

ALTER TABLE usuarios
    ADD COLUMN IF NOT EXISTS pregunta_pendiente_ts TIMESTAMPTZ;

-- 2. Verificación: debe mostrar las columnas nuevas, todas las filas
--    existentes con pregunta_pendiente / pregunta_pendiente_ts = NULL
SELECT id, whatsapp_number, nombre, pregunta_pendiente, pregunta_pendiente_ts
FROM usuarios
ORDER BY created_at DESC
LIMIT 10;
