import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
  Upload,
  FileText,
  Play,
  CheckCircle,
  Clock,
  AlertTriangle,
  Database,
  Shield,
  Zap,
  RefreshCw,
  X,
  File,
  Terminal,
  StopCircle,
} from 'lucide-react';
import Button from '../components/common/Button';
import Badge from '../components/common/Badge';
import {
  uploadPDF,
  uploadCSV,
  runPipeline,
  fetchPipelineStatus,
  fetchAuditLog,
} from '../services/api';
import { useScan } from '../context/ScanContext';
import './ScanNow.css';

const PHASE_OPTIONS = [
  { value: '123', label: 'Full Scan', desc: 'Extract rules + Execute SQL + Generate explanations' },
  { value: '12', label: 'Extract + SQL', desc: 'Parse PDF and run violation queries' },
  { value: '1', label: 'Extract Only', desc: 'Parse PDF and extract policy rules' },
  { value: '2', label: 'Validate Only', desc: 'Check data against existing rules (no PDF needed)' },
  { value: '3', label: 'Explain Only', desc: 'Generate AI explanations for violations' },
];

function ScanNow() {
  const [dragActive, setDragActive] = useState(false);
  const [uploadedFile, setUploadedFile] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [uploadedFilename, setUploadedFilename] = useState(null);
  const [scanPhase, setScanPhase] = useState('123');
  const [isScanning, setIsScanning] = useState(false);
  const [scanStatus, setScanStatus] = useState(null); // 'success' | 'error'
  const [logLines, setLogLines] = useState([]);
  const [logOffset, setLogOffset] = useState(0);
  const [recentScans, setRecentScans] = useState([]);
  const [loadingScans, setLoadingScans] = useState(true);

  const [csvDragActive, setCsvDragActive] = useState(false);
  const [uploadedCsvFile, setUploadedCsvFile] = useState(null);
  const [uploadingCsv, setUploadingCsv] = useState(false);
  const [csvStatus, setCsvStatus] = useState(null);

  const { refresh: refreshGate } = useScan();
  const pollRef = useRef(null);
  const logBoxRef = useRef(null);

  // Load recent scans from audit log
  const loadRecentScans = async () => {
    setLoadingScans(true);
    try {
      const data = await fetchAuditLog(20);
      const pipelineRuns = (data.logs || []).filter(l => l.event_type === 'PIPELINE_RUN');
      setRecentScans(pipelineRuns);
    } catch {
      // silently ignore
    } finally {
      setLoadingScans(false);
    }
  };

  useEffect(() => {
    loadRecentScans();
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, []);

  // Auto-scroll log box
  useEffect(() => {
    if (logBoxRef.current) {
      logBoxRef.current.scrollTop = logBoxRef.current.scrollHeight;
    }
  }, [logLines]);

  const stopPolling = useCallback(() => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
  }, []);

  const startPolling = useCallback(() => {
    stopPolling();
    let offset = 0;
    pollRef.current = setInterval(async () => {
      try {
        const status = await fetchPipelineStatus(offset);
        const newLines = status.log_lines || [];
        if (newLines.length > 0) {
          offset += newLines.length;
          setLogLines(prev => [...prev, ...newLines]);
          setLogOffset(offset);
        }
        if (!status.running) {
          stopPolling();
          setIsScanning(false);
          const success = status.returncode === 0;
          setScanStatus(success ? 'success' : 'error');
          loadRecentScans();
          if (success) refreshGate(); // unlock gated pages
        }
      } catch { /* ignore poll errors */ }
    }, 1500);
  }, [stopPolling]);

  const handleDrag = (e) => {
    e.preventDefault(); e.stopPropagation();
    if (e.type === 'dragenter' || e.type === 'dragover') setDragActive(true);
    else if (e.type === 'dragleave') setDragActive(false);
  };

  const handleDrop = (e) => {
    e.preventDefault(); e.stopPropagation();
    setDragActive(false);
    const files = e.dataTransfer.files;
    if (files && files[0]) handleFileSelect(files[0]);
  };

  const handleFileSelect = (file) => {
    if (!file.name.endsWith('.pdf')) {
      alert('Only PDF files are supported.');
      return;
    }
    setUploadedFile(file);
    setUploadedFilename(null);
    setScanStatus(null);
  };

  const handleFileInput = (e) => {
    if (e.target.files && e.target.files[0]) handleFileSelect(e.target.files[0]);
  };

  const removeFile = () => {
    setUploadedFile(null);
    setUploadedFilename(null);
  };

  const handleCsvDrag = (e) => {
    e.preventDefault(); e.stopPropagation();
    if (e.type === 'dragenter' || e.type === 'dragover') setCsvDragActive(true);
    else if (e.type === 'dragleave') setCsvDragActive(false);
  };

  const handleCsvDrop = (e) => {
    e.preventDefault(); e.stopPropagation();
    setCsvDragActive(false);
    const files = e.dataTransfer.files;
    if (files && files[0]) handleCsvSelect(files[0]);
  };

  const handleCsvSelect = (file) => {
    if (!file.name.endsWith('.csv')) {
      alert('Only CSV files are supported.');
      return;
    }
    setUploadedCsvFile(file);
    setCsvStatus(null);
  };

  const handleCsvInput = (e) => {
    if (e.target.files && e.target.files[0]) handleCsvSelect(e.target.files[0]);
  };

  const uploadDataset = async () => {
    if (!uploadedCsvFile) return;
    setUploadingCsv(true);
    setCsvStatus(null);
    try {
      await uploadCSV(uploadedCsvFile);
      setCsvStatus('success');
      setUploadedCsvFile(null);
    } catch (e) {
      setCsvStatus('error');
      setLogLines([`[ERROR] ${e.message}`]);
    } finally {
      setUploadingCsv(false);
    }
  };

  const formatFileSize = (bytes) => {
    if (!bytes) return '';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
  };

  const startScan = async () => {
    // Phase 2 and 3 don't need a PDF
    const needsPdf = ['1', '12', '123'].includes(scanPhase);
    if (needsPdf && !uploadedFile) return;

    setIsScanning(true);
    setScanStatus(null);
    setLogLines([]);
    setLogOffset(0);

    try {
      let filename = uploadedFilename;

      // Upload PDF first if needed
      if (needsPdf && uploadedFile && !uploadedFilename) {
        setUploading(true);
        const up = await uploadPDF(uploadedFile);
        filename = up.filename;
        setUploadedFilename(filename);
        setUploading(false);
      }

      await runPipeline(scanPhase, filename || null);
      startPolling();
    } catch (e) {
      setIsScanning(false);
      setUploading(false);
      setScanStatus('error');
      setLogLines([`[ERROR] ${e.message}`]);
    }
  };

  const needsPdf = ['1', '12', '123'].includes(scanPhase);
  const canRun = isScanning ? false : (needsPdf ? !!uploadedFile : true);

  return (
    <div className="scan-now-page">
      <div className="scan-layout">
        {/* Left Panel — Config */}
        <div className="scan-config-panel">
          {/* Upload */}
          <div className="panel-section">
            <h3 className="section-title">
              <Upload size={18} />
              Upload Policy PDF
            </h3>

            {!uploadedFile ? (
              <div
                className={`upload-zone ${dragActive ? 'active' : ''}`}
                onDragEnter={handleDrag}
                onDragLeave={handleDrag}
                onDragOver={handleDrag}
                onDrop={handleDrop}
              >
                <input
                  type="file"
                  id="file-upload"
                  accept=".pdf"
                  onChange={handleFileInput}
                  className="file-input"
                />
                <label htmlFor="file-upload" className="upload-label">
                  <div className="upload-icon"><FileText size={32} /></div>
                  <p className="upload-text">
                    <span>Click to upload</span> or drag and drop
                  </p>
                  <p className="upload-hint">PDF files only</p>
                </label>
              </div>
            ) : (
              <div className="uploaded-files">
                <div className="file-item">
                  <File size={16} />
                  <div className="file-info">
                    <span className="file-name">{uploadedFile.name}</span>
                    <span className="file-size">{formatFileSize(uploadedFile.size)}</span>
                    {uploadedFilename && (
                      <span className="file-uploaded">✓ Uploaded</span>
                    )}
                  </div>
                  <button className="remove-file-btn" onClick={removeFile}>
                    <X size={14} />
                  </button>
                </div>
              </div>
            )}

            {!needsPdf && (
              <p className="upload-skip-hint">
                ℹ️ Phase {scanPhase} doesn't require a PDF upload.
              </p>
            )}
          </div>

          {/* CSV Dataset Upload */}
          <div className="panel-section">
            <h3 className="section-title">
              <Database size={18} />
              Upload AML Dataset (CSV)
            </h3>

            {!uploadedCsvFile ? (
              <div
                className={`upload-zone ${csvDragActive ? 'active' : ''}`}
                onDragEnter={handleCsvDrag}
                onDragLeave={handleCsvDrag}
                onDragOver={handleCsvDrag}
                onDrop={handleCsvDrop}
              >
                <input
                  type="file"
                  id="csv-upload"
                  accept=".csv"
                  onChange={handleCsvInput}
                  className="file-input"
                />
                <label htmlFor="csv-upload" className="upload-label">
                  <div className="upload-icon"><FileText size={32} /></div>
                  <p className="upload-text">
                    <span>Click to upload CSV</span> or drag and drop
                  </p>
                  <p className="upload-hint">Will automatically rebuild the DuckDB schema</p>
                </label>
              </div>
            ) : (
              <div className="uploaded-files">
                <div className="file-item" style={{ flexDirection: 'column', alignItems: 'flex-start', gap: '1rem' }}>
                  <div style={{ display: 'flex', alignItems: 'center', width: '100%', justifyContent: 'space-between' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                      <File size={16} />
                      <div className="file-info">
                        <span className="file-name">{uploadedCsvFile.name}</span>
                        <span className="file-size">{formatFileSize(uploadedCsvFile.size)}</span>
                      </div>
                    </div>
                    <button className="remove-file-btn" style={{ position: 'static', alignSelf: 'flex-start' }} onClick={() => setUploadedCsvFile(null)}>
                      <X size={14} />
                    </button>
                  </div>
                  <Button
                    variant="primary"
                    size="sm"
                    fullWidth
                    loading={uploadingCsv}
                    onClick={uploadDataset}
                  >
                    {uploadingCsv ? 'Setting up DuckDB…' : 'Ingest Dataset & Create Views'}
                  </Button>
                </div>
              </div>
            )}
            {csvStatus === 'success' && (
              <p className="upload-hint" style={{ color: '#16a34a', marginTop: '0.5rem', fontWeight: 600 }}>
                ✓ Database ingested and views updated successfully!
              </p>
            )}
            {csvStatus === 'error' && (
              <p className="upload-hint" style={{ color: '#dc2626', marginTop: '0.5rem', fontWeight: 600 }}>
                ❌ Failed to ingest CSV dataset. Check log.
              </p>
            )}
          </div>

          {/* Phase Selection */}
          <div className="panel-section">
            <h3 className="section-title">
              <Zap size={18} />
              Scan Phase
            </h3>
            <div className="scan-options">
              {PHASE_OPTIONS.map(opt => (
                <label key={opt.value} className="option-item">
                  <input
                    type="radio"
                    name="scanPhase"
                    value={opt.value}
                    checked={scanPhase === opt.value}
                    onChange={(e) => setScanPhase(e.target.value)}
                  />
                  <div className="option-content">
                    <span className="option-label">{opt.label}</span>
                    <span className="option-desc">{opt.desc}</span>
                  </div>
                </label>
              ))}
            </div>
          </div>

          <Button
            variant="primary"
            size="lg"
            fullWidth
            icon={isScanning ? StopCircle : Play}
            onClick={startScan}
            loading={uploading}
            disabled={!canRun}
          >
            {uploading ? 'Uploading…' : isScanning ? 'Running…' : 'Start Scan'}
          </Button>

          {/* Status indicator */}
          {scanStatus === 'success' && (
            <div className="scan-result-banner success">
              <CheckCircle size={18} />
              Pipeline completed successfully!
            </div>
          )}
          {scanStatus === 'error' && (
            <div className="scan-result-banner error">
              <AlertTriangle size={18} />
              Pipeline exited with errors. Check the log.
            </div>
          )}
        </div>

        {/* Right Panel — Log + Recent Scans */}
        <div className="recent-scans-panel">
          {/* Live Log */}
          {(logLines.length > 0 || isScanning) && (
            <div className="scan-log-section">
              <div className="panel-header">
                <h3 className="panel-title">
                  <Terminal size={18} />
                  Pipeline Log
                  {isScanning && <span className="live-indicator">● LIVE</span>}
                </h3>
              </div>
              <div className="log-box" ref={logBoxRef}>
                {logLines.length === 0 ? (
                  <span className="log-waiting">Waiting for output…</span>
                ) : (
                  logLines.map((line, i) => (
                    <div key={i} className="log-line">{line}</div>
                  ))
                )}
              </div>
            </div>
          )}

          {/* Recent Scans */}
          <div className="panel-header" style={{ marginTop: logLines.length > 0 ? '1.5rem' : 0 }}>
            <h3 className="panel-title">
              <Database size={18} />
              Recent Pipeline Runs
            </h3>
            <Button variant="ghost" size="sm" icon={RefreshCw} onClick={loadRecentScans}>
              Refresh
            </Button>
          </div>

          <div className="scans-list">
            {loadingScans ? (
              <div className="loading-mini"><RefreshCw size={18} className="spinning" /> Loading…</div>
            ) : recentScans.length === 0 ? (
              <div className="empty-scans">
                <Clock size={32} />
                <p>No pipeline runs yet</p>
              </div>
            ) : (
              recentScans.map((scan, i) => {
                const details = scan.details || {};
                return (
                  <div key={scan.id || i} className="scan-item completed">
                    <div className="scan-item-header">
                      <div className="scan-info">
                        <span className="scan-id">#{scan.id}</span>
                        <Badge variant="green" size="sm">
                          <CheckCircle size={12} />
                          completed
                        </Badge>
                      </div>
                      <span className="scan-time">
                        {scan.ts ? scan.ts.slice(0, 19).replace('T', ' ') : '—'}
                      </span>
                    </div>
                    <div className="scan-details">
                      <div className="scan-file">
                        <Shield size={14} />
                        <span>{scan.phase || 'Pipeline'}</span>
                      </div>
                    </div>
                    {(details.rules_processed || details.violations_count !== undefined) && (
                      <div className="scan-results">
                        {details.rules_processed !== undefined && (
                          <div className="result-item">
                            <Shield size={14} />
                            <span>{details.rules_processed} rules</span>
                          </div>
                        )}
                        {details.violations_count !== undefined && (
                          <div className="result-item">
                            <AlertTriangle size={14} />
                            <span>{details.violations_count} violations</span>
                          </div>
                        )}
                        {details.duration_s !== undefined && (
                          <div className="result-item">
                            <Clock size={14} />
                            <span>{details.duration_s}s</span>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export default ScanNow;
