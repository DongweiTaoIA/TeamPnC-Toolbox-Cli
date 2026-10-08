-- Knowledge base: raw extraction layer (Phase 1-2 output). Re-runnable.
IF SCHEMA_ID('kb') IS NULL EXEC('CREATE SCHEMA kb');
GO

IF OBJECT_ID('kb.Document') IS NULL
CREATE TABLE kb.Document
(
    DocumentId    INT IDENTITY(1, 1) NOT NULL CONSTRAINT PK_kb_Document PRIMARY KEY,
    Product       VARCHAR(20)     NOT NULL,
    FileName      NVARCHAR(260)   NOT NULL,
    Prefix        VARCHAR(40)     NULL,
    Family        VARCHAR(40)     NULL,
    FormNo        VARCHAR(20)     NULL,
    Variant       VARCHAR(40)     NULL,
    VariantRaw    VARCHAR(40)     NULL,
    RevDate       DATE            NULL,
    Lang          CHAR(1)         NULL,
    GroupKey      NVARCHAR(200)   NULL,
    IsLatest      BIT             NOT NULL,
    InScope       BIT             NOT NULL,
    ParseOk       BIT             NOT NULL,
    PageCount     SMALLINT        NULL,
    PageSize      VARCHAR(40)     NULL,
    IsFillable    BIT             NOT NULL,
    FieldCount    SMALLINT        NOT NULL,
    FieldPages    VARCHAR(100)    NULL,
    Fonts         NVARCHAR(2000)  NULL,
    SizeKb        INT             NULL,
    Notes         NVARCHAR(1000)  NULL,
    Error         NVARCHAR(1000)  NULL,
    -- Extraction results (in-scope documents only)
    IsExtracted   BIT             NOT NULL CONSTRAINT DF_kb_Document_IsExtracted DEFAULT 0,
    BodyFontSize  DECIMAL(4, 1)   NULL,
    LinePitch     DECIMAL(5, 2)   NULL,
    IndentStep    DECIMAL(5, 2)   NULL,
    Coverage      DECIMAL(6, 4)   NULL,
    PageBands     JSON            NULL,
    Furniture     JSON            NULL,
    LoadedAt      DATETIME2(0)    NOT NULL CONSTRAINT DF_kb_Document_LoadedAt DEFAULT SYSUTCDATETIME(),
    CONSTRAINT UQ_kb_Document UNIQUE (Product, FileName)
);
GO

IF OBJECT_ID('kb.PageLayout') IS NULL
CREATE TABLE kb.PageLayout
(
    DocumentId  INT   NOT NULL CONSTRAINT PK_kb_PageLayout PRIMARY KEY
                      CONSTRAINT FK_kb_PageLayout_Document REFERENCES kb.Document (DocumentId) ON DELETE CASCADE,
    PageWidth   DECIMAL(7, 2) NOT NULL,
    PageHeight  DECIMAL(7, 2) NOT NULL,
    TextJson    JSON  NOT NULL,   -- fixed labels: bbox, text, font, size, bold, color
    DrawingJson JSON  NOT NULL    -- lines / boxes
);
GO

IF OBJECT_ID('kb.FormField') IS NULL
CREATE TABLE kb.FormField
(
    DocumentId  INT            NOT NULL CONSTRAINT FK_kb_FormField_Document REFERENCES kb.Document (DocumentId) ON DELETE CASCADE,
    Seq         SMALLINT       NOT NULL,
    FieldName   NVARCHAR(200)  NOT NULL,
    FieldType   VARCHAR(20)    NOT NULL,
    X0          DECIMAL(7, 1)  NOT NULL,
    Y0          DECIMAL(7, 1)  NOT NULL,
    X1          DECIMAL(7, 1)  NOT NULL,
    Y1          DECIMAL(7, 1)  NOT NULL,
    DefaultValue NVARCHAR(1000) NULL,
    Options     JSON           NULL,
    Flags       INT            NULL,
    FontSize    DECIMAL(5, 2)  NULL,
    MaxLen      INT            NULL,
    OnState     NVARCHAR(100)  NULL,
    CONSTRAINT PK_kb_FormField PRIMARY KEY (DocumentId, Seq)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_kb_FormField_Name')
    CREATE INDEX IX_kb_FormField_Name ON kb.FormField (FieldName);
GO

IF OBJECT_ID('kb.Asset') IS NULL
CREATE TABLE kb.Asset
(
    Sha1    CHAR(40)        NOT NULL CONSTRAINT PK_kb_Asset PRIMARY KEY,
    Ext     VARCHAR(10)     NOT NULL,
    Width   INT             NULL,
    Height  INT             NULL,
    Bytes   VARBINARY(MAX)  NOT NULL
);
GO

IF OBJECT_ID('kb.DocumentAsset') IS NULL
CREATE TABLE kb.DocumentAsset
(
    DocumentId INT           NOT NULL CONSTRAINT FK_kb_DocumentAsset_Document REFERENCES kb.Document (DocumentId) ON DELETE CASCADE,
    Seq        SMALLINT      NOT NULL,
    Sha1       CHAR(40)      NOT NULL CONSTRAINT FK_kb_DocumentAsset_Asset REFERENCES kb.Asset (Sha1),
    X0         DECIMAL(7, 1) NOT NULL,
    Y0         DECIMAL(7, 1) NOT NULL,
    X1         DECIMAL(7, 1) NOT NULL,
    Y1         DECIMAL(7, 1) NOT NULL,
    CONSTRAINT PK_kb_DocumentAsset PRIMARY KEY (DocumentId, Seq)
);
GO

-- Policy-page content blocks in reading order ('region' rows mark column changes).
IF OBJECT_ID('kb.Block') IS NULL
CREATE TABLE kb.Block
(
    DocumentId   INT            NOT NULL CONSTRAINT FK_kb_Block_Document REFERENCES kb.Document (DocumentId) ON DELETE CASCADE,
    Seq          SMALLINT       NOT NULL,
    BlockType    VARCHAR(20)    NOT NULL,
    PageNo       SMALLINT       NULL,
    Columns      TINYINT        NULL,
    Level        TINYINT        NULL,
    Marker       NVARCHAR(10)   NULL,
    MarkerStyle  VARCHAR(20)    NULL,
    TermJson     JSON           NULL,
    RunInJson    JSON           NULL,
    RunsJson     JSON           NULL,
    RowsJson     JSON           NULL,
    Xrefs        JSON           NULL,
    PlainText    NVARCHAR(MAX)  NULL,
    TextHash     AS CAST(HASHBYTES('SHA2_256', PlainText) AS BINARY(32)) PERSISTED,
    CONSTRAINT PK_kb_Block PRIMARY KEY (DocumentId, Seq)
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_kb_Block_TextHash')
    CREATE INDEX IX_kb_Block_TextHash ON kb.Block (TextHash) INCLUDE (BlockType);
GO

IF OBJECT_ID('kb.DefinedTerm') IS NULL
CREATE TABLE kb.DefinedTerm
(
    DocumentId INT           NOT NULL CONSTRAINT FK_kb_DefinedTerm_Document REFERENCES kb.Document (DocumentId) ON DELETE CASCADE,
    Term       NVARCHAR(200) NOT NULL,
    CONSTRAINT PK_kb_DefinedTerm PRIMARY KEY (DocumentId, Term)
);
GO
