export default function PostPreview({ title, content, imageB64 }) {
  const previewText = content
    ? content.replace(/<[^>]+>/g, '').slice(0, 300) + '...'
    : ''

  return (
    <div className="border border-gray-200 rounded-xl overflow-hidden bg-white shadow-sm">
      {imageB64 && (
        <div className="h-48 overflow-hidden bg-gray-100">
          <img
            src={`data:image/jpeg;base64,${imageB64}`}
            alt="Featured"
            className="w-full h-full object-cover"
          />
        </div>
      )}
      {!imageB64 && (
        <div className="h-48 bg-gradient-to-br from-blue-50 to-indigo-100 flex items-center justify-center">
          <svg className="w-16 h-16 text-blue-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
              d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
          </svg>
        </div>
      )}
      <div className="p-5">
        <h3 className="text-lg font-bold text-gray-900 mb-2 leading-tight">{title}</h3>
        <p className="text-gray-500 text-sm leading-relaxed">{previewText}</p>
        <div className="mt-3 pt-3 border-t border-gray-100">
          <span className="text-xs text-gray-400">
            {content ? `${content.replace(/<[^>]+>/g, '').length} znaków` : ''}
          </span>
        </div>
      </div>
    </div>
  )
}
