"""
Office Tool Registry.

Defines tools for extracting data from Office documents (Excel, Word, PowerPoint).
Files arrive as cached Paths via the asset resolution middleware — IRM decryption
has already been applied transparently.
"""

import logging

from code_execution import ToolRegistry, ToolDefinition, ToolParameter, ReturnSpec

LOGGER = logging.getLogger(__name__)


def create_office_tool_registry() -> "ToolRegistry":
    """
    Create tool registry for the office domain.

    All tools receive file assets as resolved Path objects (via
    AssetResolutionMiddleware). The tools are read-only and extract
    data from Excel, Word, and PowerPoint files.

    Returns:
        ToolRegistry with office domain tools.
    """

    registry = ToolRegistry()

    # ── Excel tools ──────────────────────────────────────────────────────

    registry.register_tool(
        ToolDefinition(
            name="read_excel_sheets",
            description=(
                "List all sheet names in an Excel workbook. "
                "Use this to discover available sheets before extracting data."
            ),
            required_parameters=[
                ToolParameter(
                    name="file",
                    type=object,
                    description="Excel file asset (qualified_name is automatically resolved to a cached Path)",
                ),
            ],
            optional_parameters=[],
            return_spec=[
                ReturnSpec(
                    name="sheets",
                    type=list,
                    description="List of sheet names in the workbook",
                ),
                ReturnSpec(
                    name="sheet_count",
                    type=int,
                    description="Total number of sheets",
                ),
            ],
            module="office_tools.tools.excel_tools",
            server_name="office",
        )
    )

    registry.register_tool(
        ToolDefinition(
            name="extract_excel_data",
            description=(
                "Extract data from a specific sheet in an Excel workbook as a table. "
                "Returns column names and rows. Optionally limit the number of rows returned."
            ),
            required_parameters=[
                ToolParameter(
                    name="file",
                    type=object,
                    description="Excel file asset (resolved to cached Path)",
                ),
            ],
            optional_parameters=[
                ToolParameter(
                    name="sheet_name",
                    type=str,
                    description="Sheet to read (default: first sheet)",
                    default=None,
                ),
                ToolParameter(
                    name="max_rows",
                    type=int,
                    description="Maximum rows to return (default: 1000, use -1 for all)",
                    default=1000,
                ),
            ],
            return_spec=[
                ReturnSpec(
                    name="columns",
                    type=list,
                    description="Column names",
                ),
                ReturnSpec(
                    name="rows",
                    type=list,
                    description="List of row dicts",
                ),
                ReturnSpec(
                    name="total_rows",
                    type=int,
                    description="Total number of rows in the sheet (before truncation)",
                ),
                ReturnSpec(
                    name="truncated",
                    type=bool,
                    description="Whether the result was truncated by max_rows",
                ),
            ],
            module="office_tools.tools.excel_tools",
            server_name="office",
        )
    )

    registry.register_tool(
        ToolDefinition(
            name="get_excel_metadata",
            description=(
                "Get metadata about an Excel workbook: sheet names, row/column counts, "
                "column data types, and file size."
            ),
            required_parameters=[
                ToolParameter(
                    name="file",
                    type=object,
                    description="Excel file asset (resolved to cached Path)",
                ),
            ],
            optional_parameters=[],
            return_spec=[
                ReturnSpec(
                    name="file_size_bytes",
                    type=int,
                    description="Size of the file on disk",
                ),
                ReturnSpec(
                    name="sheets",
                    type=list,
                    description="List of dicts with sheet name, row_count, column_count, and column dtypes",
                ),
            ],
            module="office_tools.tools.excel_tools",
            server_name="office",
        )
    )

    # ── Word tools ───────────────────────────────────────────────────────

    registry.register_tool(
        ToolDefinition(
            name="extract_word_text",
            description=("Extract all text content from a Word document (.docx), preserving paragraph structure."),
            required_parameters=[
                ToolParameter(
                    name="file",
                    type=object,
                    description="Word file asset (resolved to cached Path)",
                ),
            ],
            optional_parameters=[],
            return_spec=[
                ReturnSpec(
                    name="text",
                    type=str,
                    description="Full text content with paragraph breaks",
                ),
                ReturnSpec(
                    name="paragraph_count",
                    type=int,
                    description="Number of paragraphs",
                ),
            ],
            module="office_tools.tools.word_tools",
            server_name="office",
        )
    )

    registry.register_tool(
        ToolDefinition(
            name="extract_word_tables",
            description=(
                "Extract all tables from a Word document as lists of row dicts. "
                "Each table uses its first row as column headers."
            ),
            required_parameters=[
                ToolParameter(
                    name="file",
                    type=object,
                    description="Word file asset (resolved to cached Path)",
                ),
            ],
            optional_parameters=[],
            return_spec=[
                ReturnSpec(
                    name="tables",
                    type=list,
                    description="List of tables, each a list of row dicts",
                ),
                ReturnSpec(
                    name="table_count",
                    type=int,
                    description="Number of tables found",
                ),
            ],
            module="office_tools.tools.word_tools",
            server_name="office",
        )
    )

    # ── PowerPoint tools ─────────────────────────────────────────────────

    registry.register_tool(
        ToolDefinition(
            name="extract_slides_text",
            description=(
                "Extract text from all slides in a PowerPoint presentation. Returns text organized by slide number."
            ),
            required_parameters=[
                ToolParameter(
                    name="file",
                    type=object,
                    description="PowerPoint file asset (resolved to cached Path)",
                ),
            ],
            optional_parameters=[],
            return_spec=[
                ReturnSpec(
                    name="slides",
                    type=list,
                    description="List of dicts with slide_number and text content",
                ),
                ReturnSpec(
                    name="slide_count",
                    type=int,
                    description="Total number of slides",
                ),
            ],
            module="office_tools.tools.powerpoint_tools",
            server_name="office",
        )
    )

    registry.register_tool(
        ToolDefinition(
            name="extract_slide_notes",
            description=("Extract speaker notes from all slides in a PowerPoint presentation."),
            required_parameters=[
                ToolParameter(
                    name="file",
                    type=object,
                    description="PowerPoint file asset (resolved to cached Path)",
                ),
            ],
            optional_parameters=[],
            return_spec=[
                ReturnSpec(
                    name="notes",
                    type=list,
                    description="List of dicts with slide_number and notes text",
                ),
                ReturnSpec(
                    name="slides_with_notes",
                    type=int,
                    description="Number of slides that have speaker notes",
                ),
            ],
            module="office_tools.tools.powerpoint_tools",
            server_name="office",
        )
    )

    # ── PDF tools ────────────────────────────────────────────────────────

    registry.register_tool(
        ToolDefinition(
            name="extract_pdf_text",
            description=("Extract all text content from a PDF document, organized by page."),
            required_parameters=[
                ToolParameter(
                    name="file",
                    type=object,
                    description="PDF file asset (resolved to cached Path)",
                ),
            ],
            optional_parameters=[],
            return_spec=[
                ReturnSpec(
                    name="text",
                    type=str,
                    description="Full text content of the PDF",
                ),
                ReturnSpec(
                    name="page_count",
                    type=int,
                    description="Number of pages",
                ),
                ReturnSpec(
                    name="pages",
                    type=list,
                    description="List of dicts with page_number and text",
                ),
            ],
            module="office_tools.tools.pdf_tools",
            server_name="office",
        )
    )

    registry.register_tool(
        ToolDefinition(
            name="extract_pdf_tables",
            description=("Extract tables from a PDF document. Returns tabular data with headers and rows per page."),
            required_parameters=[
                ToolParameter(
                    name="file",
                    type=object,
                    description="PDF file asset (resolved to cached Path)",
                ),
            ],
            optional_parameters=[],
            return_spec=[
                ReturnSpec(
                    name="tables",
                    type=list,
                    description="List of table dicts with page_number, headers, and rows",
                ),
                ReturnSpec(
                    name="table_count",
                    type=int,
                    description="Number of tables found",
                ),
            ],
            module="office_tools.tools.pdf_tools",
            server_name="office",
        )
    )

    registry.register_tool(
        ToolDefinition(
            name="extract_pdf_markdown",
            description=(
                "Extract PDF content as Markdown-formatted text, preserving headings, "
                "lists, and tables. Best for feeding content to an LLM."
            ),
            required_parameters=[
                ToolParameter(
                    name="file",
                    type=object,
                    description="PDF file asset (resolved to cached Path)",
                ),
            ],
            optional_parameters=[],
            return_spec=[
                ReturnSpec(
                    name="markdown",
                    type=str,
                    description="Full document as Markdown text",
                ),
                ReturnSpec(
                    name="page_count",
                    type=int,
                    description="Number of pages",
                ),
            ],
            module="office_tools.tools.pdf_tools",
            server_name="office",
        )
    )

    registry.register_tool(
        ToolDefinition(
            name="get_pdf_metadata",
            description=("Get metadata about a PDF file: page count, title, author, and file size."),
            required_parameters=[
                ToolParameter(
                    name="file",
                    type=object,
                    description="PDF file asset (resolved to cached Path)",
                ),
            ],
            optional_parameters=[],
            return_spec=[
                ReturnSpec(
                    name="page_count",
                    type=int,
                    description="Number of pages",
                ),
                ReturnSpec(
                    name="metadata",
                    type=dict,
                    description="PDF metadata (title, author, subject, creator, etc.)",
                ),
                ReturnSpec(
                    name="file_size_bytes",
                    type=int,
                    description="Size of the file on disk",
                ),
            ],
            module="office_tools.tools.pdf_tools",
            server_name="office",
        )
    )

    LOGGER.info(f"Registered {len(registry.tools)} office tools")
    return registry
