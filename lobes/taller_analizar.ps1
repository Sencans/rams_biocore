# Analizador estatico del taller de scripts de R.A.M.S. BIO-CORE (v0.2.2).
# docs/V022_CORREO_DRIVE_SCRIPTS.md seccion 9.4; lo invoca lobes/taller_scripts.py.
#
# Lee el .ps1 indicado como TEXTO y lo pasa a Parser::ParseInput: nunca lo ejecuta, nunca lo
# dot-sourcea y nunca lo pasa por -Command. Emite por la salida estandar un JSON compacto (UTF-8 sin
# BOM) con lo que encuentra en el AST:
#   {errores, comandos, miembros, tipos, variables, usings}
#   errores   = [{mensaje, linea}]
#   comandos  = [{nombre, dinamico, operador, params, literales, linea}]
#   miembros  = [{objeto, miembro, estatico, linea}]
#   tipos     = [nombre completo de cada tipo .NET referenciado]
#   variables = [nombre de cada variable usada]
#   usings    = [texto de cada sentencia using]
# El riesgo lo decide el lado Python (catalogo normativo); aqui solo se extraen hechos.
#
# Uso:
#   powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File taller_analizar.ps1 -Ruta <archivo>
# -ExecutionPolicy Bypass solo afecta a ESTE proceso (nunca se usa Set-ExecutionPolicy).
#
# Codigos de salida: 0 = JSON emitido; 2 = no se pudo leer la ruta; otro = error inesperado
# (el lado Python cae entonces al analisis heuristico, mas conservador).
#
# Este archivo es SOLO ASCII (comentarios en espanol sin tildes): PowerShell 5.1 lee los .ps1 sin
# BOM como ANSI y asi lo interpreta igual en cualquier pagina de codigos.
param([Parameter(Mandatory = $true)][string]$Ruta)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Off

# 1) Leer el objetivo como texto (UTF-8; ReadAllText reconoce y quita el BOM si lo hay).
try {
    $codigo = [System.IO.File]::ReadAllText($Ruta, [System.Text.UTF8Encoding]::new($false))
} catch {
    exit 2
}

# 2) Parsear SIN ejecutar: los errores de sintaxis y los tokens vuelven por referencia.
$tokens = $null
$errores = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($codigo, [ref]$tokens, [ref]$errores)

$tipoCadena = [System.Management.Automation.Language.StringConstantExpressionAst]
$tipoExpandible = [System.Management.Automation.Language.ExpandableStringExpressionAst]
$tipoArreglo = [System.Management.Automation.Language.ArrayLiteralAst]
$tipoParametro = [System.Management.Automation.Language.CommandParameterAst]

# Valor de un argumento literal (cadena constante o expandible sin expandir); $null si no lo es.
function Get-ValorLiteral($elemento) {
    if ($elemento -is $tipoCadena -or $elemento -is $tipoExpandible) {
        return [string]$elemento.Value
    }
    return $null
}

# 3) Comandos: nombre (nulo si es dinamico: & $x, & ('a'+'b')), operador, parametros y literales.
$comandos = foreach ($c in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)) {
    $nombre = $c.GetCommandName()
    $params = @($c.CommandElements | Where-Object { $_ -is $tipoParametro } | ForEach-Object { $_.ParameterName })
    # Literales de los argumentos (se salta el primer elemento, que es el nombre del comando).
    $literales = New-Object System.Collections.Generic.List[string]
    foreach ($e in @($c.CommandElements | Select-Object -Skip 1)) {
        $valor = Get-ValorLiteral $e
        if ($null -ne $valor) {
            $literales.Add($valor)
        } elseif ($e -is $tipoArreglo) {
            foreach ($x in $e.Elements) {
                $valor = Get-ValorLiteral $x
                if ($null -ne $valor) { $literales.Add($valor) }
            }
        } elseif ($e -is $tipoParametro -and $null -ne $e.Argument) {
            $valor = Get-ValorLiteral $e.Argument
            if ($null -ne $valor) { $literales.Add($valor) }
        }
    }
    [ordered]@{
        nombre    = $nombre
        dinamico  = ($null -eq $nombre)
        operador  = $c.InvocationOperator.ToString()
        params    = $params
        literales = $literales.ToArray()
        linea     = $c.Extent.StartLineNumber
    }
}

# 4) Llamadas a metodos .NET ([Convert]::FromBase64String, $wc.DownloadString(...), ...).
$miembros = foreach ($m in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true)) {
    [ordered]@{
        objeto   = $m.Expression.Extent.Text
        miembro  = $m.Member.Extent.Text
        estatico = $m.Static
        linea    = $m.Extent.StartLineNumber
    }
}

# 5) Tipos, variables y usings.
$tipos = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.TypeExpressionAst] -or
            $n -is [System.Management.Automation.Language.TypeConstraintAst] }, $true) |
    ForEach-Object { $_.TypeName.FullName } | Sort-Object -Unique)
$variables = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.VariableExpressionAst] }, $true) |
    ForEach-Object { $_.VariablePath.UserPath } | Sort-Object -Unique)
$usings = @($ast.UsingStatements | ForEach-Object { $_.Extent.Text })

$resultado = [ordered]@{
    errores   = @($errores | ForEach-Object { [ordered]@{ mensaje = $_.Message; linea = $_.Extent.StartLineNumber } })
    comandos  = @($comandos)
    miembros  = @($miembros)
    tipos     = $tipos
    variables = $variables
    usings    = $usings
}
$json = $resultado | ConvertTo-Json -Depth 6 -Compress

# 6) Escribir los bytes UTF-8 directamente (sin depender de la pagina de codigos de la consola, que
#    puede no existir con CREATE_NO_WINDOW).
$bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
$salida = [System.Console]::OpenStandardOutput()
$salida.Write($bytes, 0, $bytes.Length)
$salida.Flush()
exit 0
